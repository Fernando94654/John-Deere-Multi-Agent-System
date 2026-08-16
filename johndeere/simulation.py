"""The engine: builds the world, runs the tick loop, emits snapshots.

This module prints nothing and draws nothing. Frontends observe a run through
the `on_tick` callback, which hands them an immutable `Snapshot` — the same
contract the console and the 2D view are both written against.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Callable, Optional

from .agents.grain_cart import CartState, GrainCart
from .agents.harvester import Harvester, HarvesterState
from .config import BLOCKED_TICKS_BEFORE_REROUTE, HARVESTER_TANK, SimulationConfig
from .coordination.dispatcher import Dispatcher
from .coordination.traffic import TrafficControl
from .metrics import FleetMetrics, collect
from .planning.coverage import zone_work_plan
from .planning.partition import partition_zones
from .planning.pathfinding import a_star, manhattan, reachable_cells
from .world.field import Field
from .world.grid import Cell, Grid, generate_grid


@dataclass(frozen=True)
class AgentView:
    """What a frontend needs to draw one machine."""

    id: int
    label: str
    position: Cell
    state: str
    load: int
    capacity: int
    #: For a cart under a harvester's spout, that harvester's id.
    link: Optional[int] = None
    #: Which way the machine is facing, as a (row, col) step.
    heading: tuple[int, int] = (0, 1)


@dataclass(frozen=True)
class Snapshot:
    """The state of the world after one tick."""

    tick: int
    harvesters: tuple[AgentView, ...]
    carts: tuple[AgentView, ...]
    harvested_cells: tuple[Cell, ...]
    metrics: FleetMetrics


@dataclass
class SimulationResult:
    """The outcome of a finished run."""

    ticks: int
    metrics: FleetMetrics
    harvesters: list[Harvester]
    carts: list[GrainCart]
    zones: list[set[Cell]]
    initial_grid: Grid
    unreachable_food: int
    food_left_reachable: int
    completed: bool
    collisions: int = 0
    obstacle_violations: int = 0
    snapshots: list[Snapshot] = dataclass_field(default_factory=list)


class Simulation:
    """A harvesting campaign: harvesters, grain carts and one farm."""

    def __init__(self, config: SimulationConfig):
        config.validate()
        self.config = config
        self.farm: Cell = (0, 0)
        grid = generate_grid(
            config.rows,
            config.cols,
            food_ratio=config.food_ratio,
            min_obstacles=config.min_obstacles,
            max_obstacles=config.max_obstacles,
            seed=config.seed,
            farm=self.farm,
            border=config.border,
        )
        self.initial_grid: Grid = [row[:] for row in grid]
        self.field = Field(grid, self.farm)

        self.reachable = reachable_cells(self.field, self.farm)
        self.unreachable_food = sum(
            1 for cell in self.field.food_cells() if cell not in self.reachable
        )
        self.zones = partition_zones(self.field, self.reachable, config.harvesters)

        self.harvesters = [
            Harvester(i, self.farm, zone, zone_work_plan(self.field, zone))
            for i, zone in enumerate(self.zones)
        ]
        self.by_id = {h.id: h for h in self.harvesters}
        self.carts = [GrainCart(i, self.farm) for i in range(config.carts)]

        self.dispatcher = Dispatcher()
        self.traffic = TrafficControl(depot=self.farm)
        self.tick = 0
        self.delivered = 0
        self.collisions = 0
        self.obstacle_violations = 0

    # --- helpers ----------------------------------------------------------
    @property
    def agents(self) -> list:
        return [*self.harvesters, *self.carts]

    @property
    def work_pending(self) -> bool:
        """True while any harvester still has crop to cut or grain to hand over."""
        return any(
            h.state is not HarvesterState.DONE and (h.plan or h.load > 0)
            for h in self.harvesters
        )

    def food_left_reachable(self) -> int:
        return sum(1 for cell in self.field.food_cells() if cell in self.reachable)

    def finished(self) -> bool:
        """True when the campaign is over: field cut, grain delivered, everyone home."""
        return (
            self.food_left_reachable() == 0
            and all(h.state is HarvesterState.DONE for h in self.harvesters)
            and all(
                c.load == 0 and c.position == self.farm and c.state is CartState.IDLE
                for c in self.carts
            )
        )

    def _cart_beside(self, harvester: Harvester) -> Optional[Cell]:
        """Where this harvester's assigned cart is, once it has pulled alongside."""
        if harvester.cart_id is None:
            return None
        cart = self.carts[harvester.cart_id]
        if cart.target_id != harvester.id:
            return None
        return cart.position if manhattan(cart.position, harvester.position) == 1 else None

    def _priority(self, agent) -> int:
        """Right of way, lowest number first: harvesters by id, then carts.

        Two machines heading into each other both step aside on the same tick
        and end up face to face again — a livelock that only breaks if one of
        them has the right of way and simply holds its line.
        """
        return self.agents.index(agent)

    def _has_free_exit(self, agent) -> bool:
        """True if `agent` has at least one drivable neighbour free this tick."""
        return any(
            self.traffic.blocker_at(cell) is None
            for cell in self.field.neighbors(agent.position, agent.blocked_by_crop)
        )

    def _agent_at(self, label: Optional[str]):
        return next((a for a in self.agents if a.label == label), None)

    def _move(self, agent, productive: bool = False) -> None:
        """Drive one cell if the traffic control allows it.

        `productive` marks a machine that is doing work while it stands still —
        a cart parked under the spout is unloading, not idling.
        """
        target = agent.next_cell
        if target is None:
            agent.hold(productive=productive)
            return
        if self.traffic.claim(agent.label, target, agent.position):
            agent.advance()
            return

        agent.blocked_ticks += 1
        if agent.blocked_ticks < BLOCKED_TICKS_BEFORE_REROUTE:
            agent.hold(productive=productive)
            return
        if not self._give_way(agent, target):
            agent.hold(productive=productive)

    def _give_way(self, agent, target: Cell) -> bool:
        """Try to get a stuck machine moving again; returns True if it moved."""
        blocker = self._agent_at(self.traffic.blocker_at(target))
        if (
            blocker is not None
            and blocker.route
            and self._has_free_exit(blocker)
            and self._priority(agent) < self._priority(blocker)
        ):
            # Right of way: the other machine is going somewhere and has room to
            # step aside, so it is the one that must move. Claiming right of way
            # over a machine that is boxed in — pinned between a rock and another
            # machine, as happens along the field edge — would leave the whole
            # queue waiting on the one that cannot move, so that case falls
            # through and this machine drives around instead.
            return False

        if agent.route:
            # The way round has to avoid *every* other machine, not just the one
            # in front: a detour that merely swings into the next machine in the
            # queue re-creates the jam one cell along. The farmyard and the goal
            # itself stay open — the depot is shared, and blocking the
            # destination would make the search fail instead of routing around.
            goal = agent.route[-1]
            others = tuple(
                other.position
                for other in self.agents
                if other is not agent
                and other.position != self.farm
                and other.position != goal
            )
            detour = a_star(
                self.field, agent.position, goal, (target, *others), agent.blocked_by_crop
            )
            if detour:
                agent.follow(detour)
                agent.blocked_ticks = 0
                return False  # re-routed; it will drive the new way next tick

        # No way around: pull over to any free cell so the other machine can pass.
        for cell in self.field.neighbors(agent.position, agent.blocked_by_crop):
            if cell != target and self.traffic.claim(agent.label, cell, agent.position):
                agent.follow([cell])
                agent.advance()
                agent.follow(None)
                return True

        if isinstance(agent, Harvester):
            # Somebody is parked on the very cell to cut: work elsewhere first.
            agent.defer_target()
            agent.blocked_ticks = 0
        return False

    # --- the tick ---------------------------------------------------------
    def step(self) -> Snapshot:
        """Advance the whole world by one tick and return the new snapshot."""
        self.tick += 1

        for harvester in self.harvesters:
            # A harvester calls for a cart at the threshold, and also when it has
            # finished its zone holding a part-load that is under the threshold.
            waiting_with_grain = harvester.state is HarvesterState.WAITING_CART
            if (harvester.wants_cart or waiting_with_grain) and harvester.load > 0:
                self.dispatcher.post(harvester, self.tick)
        self.dispatcher.run_auctions(self.field, self.by_id, self.carts, self.tick)

        # Grain moves before anybody drives. A tank that hit 100% last tick then
        # already has room by the time its harvester decides, so a coupled pair
        # never loses a tick to being full.
        unloading = self._transfer_grain()

        self.traffic.begin_tick(self.agents)
        harvested: list[Cell] = []

        for harvester in self.harvesters:
            harvester.decide(self.field, self._cart_beside(harvester))
            if harvester.state in (HarvesterState.HARVESTING, HarvesterState.TO_ZONE):
                self._move(harvester)
                cell = harvester.work(self.field)
                if cell is not None:
                    harvested.append(cell)
            elif harvester.state is HarvesterState.RETURNING:
                self._move(harvester)
                harvester.decide(self.field, self._cart_beside(harvester))
            else:
                # Waiting, turning on the spot or being emptied: all stand still.
                harvester.hold()

        for cart in self.carts:
            target = self.by_id.get(cart.target_id) if cart.target_id is not None else None
            cart.decide(self.field, target, self.work_pending)

            if cart.state is CartState.UNLOADING:
                self.delivered += cart.unload()
                cart.hold(productive=True)
            else:
                # A coupled cart drives too: it runs alongside the harvester to
                # stay under the spout while the grain flows.
                self._move(cart, productive=cart.id in unloading)

        self._audit()
        snapshot = Snapshot(
            tick=self.tick,
            harvesters=tuple(self._view(h) for h in self.harvesters),
            carts=tuple(self._view(c) for c in self.carts),
            harvested_cells=tuple(harvested),
            metrics=self._metrics(),
        )
        return snapshot

    def _transfer_grain(self) -> set[int]:
        """Move grain across every coupled pair; returns the carts that worked."""
        working: set[int] = set()
        for cart in self.carts:
            if cart.target_id is None:
                continue
            if cart.state not in (CartState.TO_HARVESTER, CartState.TRANSFERRING):
                continue
            harvester = self.by_id[cart.target_id]
            if not cart.docked(harvester) or harvester.load == 0:
                continue
            # Being under the spout *is* transferring: waiting for the state to
            # be set at the end of the tick would cost the harvester a stop on
            # every single docking.
            cart.state = CartState.TRANSFERRING
            if cart.pull_grain(harvester) > 0:
                working.add(cart.id)
            if harvester.load == 0:
                self.dispatcher.close(harvester)
                cart.release()
            elif cart.free_capacity == 0:
                # Full before the tank was empty: the harvester needs another cart.
                self.dispatcher.reopen(harvester.id)
                cart.release()
        return working

    def _audit(self) -> None:
        """Check the two hard invariants of the world every tick."""
        # Machines parked in the farmyard are queueing, not colliding.
        positions = [a.position for a in self.agents if a.position != self.farm]
        self.collisions += len(positions) - len(set(positions))
        self.obstacle_violations += sum(
            1 for cell in positions if not self.field.passable(cell)
        )

    def _view(self, agent) -> AgentView:
        capacity = HARVESTER_TANK
        link = None
        if isinstance(agent, GrainCart):
            capacity = agent.free_capacity + agent.load
            if agent.target_id is not None and agent.docked(self.by_id[agent.target_id]):
                link = agent.target_id
        return AgentView(
            id=agent.id,
            label=agent.label,
            position=agent.position,
            state=agent.state.value,
            load=agent.load,
            capacity=capacity,
            link=link,
            heading=agent.heading,
        )

    def _metrics(self) -> FleetMetrics:
        return collect(
            self.agents,
            ticks=self.tick,
            harvested=sum(h.harvested for h in self.harvesters),
            delivered=self.delivered,
            in_transit=sum(h.load for h in self.harvesters)
            + sum(c.load for c in self.carts),
            traffic_refusals=self.traffic.refusals,
        )

    def run(self, on_tick: Optional[Callable[[Snapshot], None]] = None) -> SimulationResult:
        """Run until the campaign finishes or `max_ticks` runs out."""
        snapshots: list[Snapshot] = []
        while not self.finished() and self.tick < self.config.max_ticks:
            snapshot = self.step()
            snapshots.append(snapshot)
            if on_tick is not None:
                on_tick(snapshot)

        return SimulationResult(
            ticks=self.tick,
            metrics=self._metrics(),
            harvesters=self.harvesters,
            carts=self.carts,
            zones=self.zones,
            initial_grid=self.initial_grid,
            unreachable_food=self.unreachable_food,
            food_left_reachable=self.food_left_reachable(),
            completed=self.finished(),
            collisions=self.collisions,
            obstacle_violations=self.obstacle_violations,
            snapshots=snapshots,
        )


def run(on_tick: Optional[Callable[[Snapshot], None]] = None, **kwargs) -> SimulationResult:
    """Convenience entry point: `run(rows=18, cols=24, harvesters=3, carts=2)`."""
    return Simulation(SimulationConfig(**kwargs)).run(on_tick)
