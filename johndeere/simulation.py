"""The engine: builds the world, runs the tick loop, emits snapshots.

This module prints nothing and draws nothing. Frontends observe a run through
the `on_tick` callback, which hands them an immutable `Snapshot` — the same
contract the console and the 2D view are both written against.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field as dataclass_field
from typing import Callable, Optional

from .agents.grain_cart import CartState, GrainCart
from .agents.harvester import Harvester, HarvesterState
from .config import (
    BLOCKED_TICKS_BEFORE_REROUTE,
    HARVESTER_TANK,
    Policy,
    SimulationConfig,
)
from .coordination.dispatcher import Dispatcher
from .coordination.traffic import TrafficControl
from .metrics import FleetMetrics, collect
from .planning.coverage import zone_work_plan
from .planning.partition import crop_only, partition_zones, repartition
from .planning.pathfinding import a_star, manhattan, reachable_cells
from .world.field import Field
from .world.grid import Cell, Grid, generate_grid

#: Ticks of history behind the rolling idle ratio the supervisor reads.
IDLE_WINDOW = 40


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
        # The day's target, fixed at sunrise: what progress is measured against.
        self.crop_total = sum(
            1 for cell in self.field.food_cells() if cell in self.reachable
        )
        self.zones = partition_zones(self.field, self.reachable, config.harvesters)

        # One shared object, so retuning it reaches the harvesters and the auction.
        self.policy = Policy()

        self.harvesters = [
            Harvester(i, self.farm, zone, zone_work_plan(self.field, zone), self.policy)
            for i, zone in enumerate(self.zones)
        ]
        self.by_id = {h.id: h for h in self.harvesters}
        self.carts = [GrainCart(i, self.farm) for i in range(config.carts)]

        self.dispatcher = Dispatcher(policy=self.policy)
        self.traffic = TrafficControl(depot=self.farm)
        self.tick = 0
        self.delivered = 0
        self.collisions = 0
        self.obstacle_violations = 0
        self.rebalances = 0
        self._idle_window: deque[int] = deque(maxlen=IDLE_WINDOW)
        self._idle_total = 0

    # --- helpers ----------------------------------------------------------
    @property
    def agents(self) -> list:
        return [*self.harvesters, *self.carts]

    @property
    def work_pending(self) -> bool:
        """True while any harvester still has crop to cut or grain to hand over."""
        return any(
            h.state is not HarvesterState.DONE
            and not h.disabled
            and (h.plan or h.load > 0)
            for h in self.harvesters
        )

    @property
    def disabled_cells(self) -> set[Cell]:
        """Where the broken-down machines stand: nobody drives or cuts there."""
        return {h.position for h in self.harvesters if h.disabled}

    def food_left_reachable(self) -> int:
        """Crop that somebody can still get to and cut.

        Crop pinned under a machine that has broken down no longer counts: it
        cannot be reached, and waiting for it would keep the campaign open
        forever.
        """
        stuck = self.disabled_cells
        return sum(
            1
            for cell in self.field.food_cells()
            if cell in self.reachable and cell not in stuck
        )

    def finished(self) -> bool:
        """True when the campaign is over: field cut, grain delivered, everyone home.

        A broken-down machine is parked for good, so it counts as finished
        wherever it stands. The grain still in its tank is stranded, and the
        metrics report it as such rather than pretending it was delivered.
        """
        return (
            self.food_left_reachable() == 0
            # The load check catches grain that reached the farm uncredited.
            and all(
                h.disabled or (h.state is HarvesterState.DONE and h.load == 0)
                for h in self.harvesters
            )
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

    def _release_cart(self, harvester: Harvester) -> None:
        """Cancel the pairing, so the cart stops chasing a machine that left."""
        if harvester.cart_id is None:
            self.dispatcher.close(harvester)
            return

        # `close` clears `cart_id`, so the cart has to be looked up first.
        cart = self.carts[harvester.cart_id]
        self.dispatcher.close(harvester)
        cart.release()

    @property
    def idle_ratio(self) -> float:
        """Share of machine-ticks spent waiting over the recent window.

        The headline number the supervisor watches: it names the logistics
        bottleneck long before the total time does.
        """
        if not self._idle_window:
            return 0.0
        return sum(self._idle_window) / (len(self._idle_window) * len(self.agents))

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


    # --- supervision ------------------------------------------------------
    def rebalance(self) -> dict:
        """Redraw the work zones over the crop that is still standing.

        The opening partition splits the field by area, which is the right call
        when nothing has been cut yet. Halfway through a campaign area and work
        have come apart: a machine can own a quarter of the map and have three
        cells left on it. Re-running the split weighted by standing crop, seeded
        where the machines actually are, hands the leftover work to whoever can
        reach it — including a machine that had already finished and parked.
        """
        active = [h for h in self.harvesters if not h.disabled]
        if not active:
            return {"rebalanced": 0, "zones": []}

        pool = self.reachable - self.disabled_cells
        zones = repartition(
            self.field,
            pool,
            [h.position for h in active],
            weight=crop_only(self.field),
        )

        for harvester, zone in zip(active, zones):
            harvester.zone = zone
            harvester.plan = zone_work_plan(self.field, zone)
            harvester.follow(None)
            if harvester.plan and harvester.state in (
                HarvesterState.DONE,
                HarvesterState.RETURNING,
            ):
                # A machine that had called it a day goes back out to help.
                harvester.state = HarvesterState.TO_ZONE

        self.rebalances += 1
        self.zones = [h.zone for h in self.harvesters]
        return {
            "rebalanced": len(active),
            "zones": [
                {"harvester": h.label, "crop": len(h.plan)} for h in active
            ],
        }

    def disable(self, harvester_id: int) -> bool:
        """Break a harvester down where it stands; returns False if already broken.

        It stops deciding, driving and burning fuel, but stays on its cell as an
        obstacle everybody else has to drive around. Its cart is released, the
        grain in its tank is stranded, and its zone is handed to the others.
        """
        harvester = self.by_id[harvester_id]
        if harvester.disabled:
            return False
        self._release_cart(harvester)
        harvester.state = HarvesterState.DISABLED
        harvester.follow(None)
        harvester.unloading = False
        # Its claim goes with it, or the screen keeps a region nobody works.
        harvester.zone = set()
        harvester.plan = []
        self.rebalance()
        return True

    def repair(self, harvester_id: int) -> bool:
        """Put a broken harvester back to work; returns False if it was not broken."""
        harvester = self.by_id[harvester_id]
        if not harvester.disabled:
            return False
        harvester.state = HarvesterState.TO_ZONE
        self.rebalance()
        return True

    def prioritize(self, top_left: Cell, bottom_right: Cell) -> int:
        """Bring the crop inside a rectangle to the front of every work plan.

        A plan is just a list of cells, so this is a stable partition of it: the
        serpentine order survives inside and outside the region, and the fleet
        visibly converges on the strip that was asked for.
        """
        row_from, row_to = sorted((top_left[0], bottom_right[0]))
        col_from, col_to = sorted((top_left[1], bottom_right[1]))

        def inside(cell: Cell) -> bool:
            return row_from <= cell[0] <= row_to and col_from <= cell[1] <= col_to

        promoted = 0
        for harvester in self.harvesters:
            if harvester.disabled or not harvester.plan:
                continue
            first = [cell for cell in harvester.plan if inside(cell)]
            if not first:
                continue
            harvester.plan = first + [c for c in harvester.plan if not inside(c)]
            harvester.follow(None)
            promoted += len(first)
        return promoted

    def add_cart(self) -> int:
        """Put one more grain cart on the field; returns its id.

        Carts are looked up by `self.carts[id]`, so ids have to stay equal to
        the index. Appending keeps that true — removing one would not, which is
        why the fleet can only grow.
        """
        cart = GrainCart(len(self.carts), self.farm)
        self.carts.append(cart)
        self.config.carts = len(self.carts)
        return cart.id

    def set_policy(
        self,
        request_threshold: Optional[float] = None,
        wait_weight: Optional[float] = None,
    ) -> dict:
        """Retune the coordination knobs mid-campaign, leaving the rest alone."""
        candidate = Policy(
            request_threshold=(
                self.policy.request_threshold
                if request_threshold is None
                else request_threshold
            ),
            wait_weight=(
                self.policy.wait_weight if wait_weight is None else wait_weight
            ),
        )
        candidate.validate()
        # Mutated in place: the harvesters and the auction share this object.
        self.policy.request_threshold = candidate.request_threshold
        self.policy.wait_weight = candidate.wait_weight
        return {
            "request_threshold": self.policy.request_threshold,
            "wait_weight": self.policy.wait_weight,
        }

    def zone_map(self) -> list[int]:
        """Row-major owner per cell: the harvester's id, or -1 for unowned."""
        owners = [-1] * (self.field.rows * self.field.cols)
        for harvester in self.harvesters:
            for row, col in harvester.zone:
                owners[row * self.field.cols + col] = harvester.id
        return owners

    def diagnostics(self) -> dict:
        """The view a supervisor reads before deciding anything.

        Deliberately not the `Snapshot`: that one is shaped for drawing the
        world, this one for judging it — what is left to do, who is waiting on
        whom, and how much of the fleet is standing still.
        """
        return {
            "tick": self.tick,
            "finished": self.finished(),
            "crop_left": self.food_left_reachable(),
            "crop_total": self.crop_total,
            # Given directly because a share of the field is what an operator
            # asks about, and models are unreliable at arithmetic.
            "progress_pct": round(
                100 * (self.crop_total - self.food_left_reachable())
                / max(self.crop_total, 1)
            ),
            "delivered": self.delivered,
            "crop_unreachable": self.unreachable_food,
            "idle_ratio": round(self.idle_ratio, 3),
            "rebalances": self.rebalances,
            "policy": {
                "request_threshold": self.policy.request_threshold,
                "wait_weight": self.policy.wait_weight,
            },
            "harvesters": [
                {
                    "id": h.label,
                    "state": h.state.value,
                    "position": {"row": h.position[0], "column": h.position[1]},
                    "load": h.load,
                    "capacity": HARVESTER_TANK,
                    "crop_left": len(h.plan),
                    "waiting_ticks": h.waiting_ticks,
                    "idle_ticks": h.idle_ticks,
                    "cart": (
                        None if h.cart_id is None else self.carts[h.cart_id].label
                    ),
                }
                for h in self.harvesters
            ],
            "carts": [
                {
                    "id": c.label,
                    "state": c.state.value,
                    "position": {"row": c.position[0], "column": c.position[1]},
                    "load": c.load,
                    "capacity": c.load + c.free_capacity,
                    "serving": (
                        None
                        if c.target_id is None
                        else self.by_id[c.target_id].label
                    ),
                    "idle_ticks": c.idle_ticks,
                }
                for c in self.carts
            ],
            "open_requests": [
                self.by_id[r.harvester_id].label
                for r in self.dispatcher.pending
            ],
        }

    # --- the tick ---------------------------------------------------------
    def step(self) -> Snapshot:
        """Advance the whole world by one tick and return the new snapshot."""
        self.tick += 1

        for harvester in self.harvesters:
            # One heading home empties its own tank; a broken one never docks at all.
            going_home = harvester.disabled or harvester.state in (
                HarvesterState.RETURNING,
                HarvesterState.DONE,
            )
            if harvester.wants_cart and harvester.load > 0 and not going_home:
                self.dispatcher.post(harvester, self.tick)
        # Put back requests whose cart walked away, or the bidding never sees them.
        self.dispatcher.sync(self.carts)
        stuck = tuple(self.disabled_cells)
        self.dispatcher.run_auctions(self.field, self.by_id, self.carts, self.tick, stuck)

        # Grain moves before anybody drives. A tank that hit 100% last tick then
        # already has room by the time its harvester decides, so a coupled pair
        # never loses a tick to being full.
        unloading = self._transfer_grain()

        self.traffic.begin_tick(self.agents)
        harvested: list[Cell] = []

        for harvester in self.harvesters:
            if harvester.disabled:
                # Broken: no decision, drive or fuel, but it still blocks its cell.
                continue

            harvester.decide(self.field, self._cart_beside(harvester))
            if harvester.state is HarvesterState.RETURNING:
                self._release_cart(harvester)

            if harvester.state in (HarvesterState.HARVESTING, HarvesterState.TO_ZONE):
                self._move(harvester)
                cell = harvester.work(self.field)
                if cell is not None:
                    harvested.append(cell)
            elif harvester.state is HarvesterState.RETURNING:
                self._move(harvester)
                # Grain carried home counts as delivered just like a cart's load.
                if harvester.position == self.farm and harvester.load > 0:
                    self.delivered += harvester.receive_from_tank(harvester.load)
                harvester.decide(self.field, self._cart_beside(harvester))
            else:
                # Emptying is work on both sides; waiting and turning are not.
                harvester.hold(productive=harvester.state is HarvesterState.UNLOADING)

        for cart in self.carts:
            target = self.by_id.get(cart.target_id) if cart.target_id is not None else None
            cart.decide(self.field, target, self.work_pending, stuck)

            if cart.state is CartState.UNLOADING:
                self.delivered += cart.unload()
                cart.hold(productive=True)
            else:
                # A coupled cart drives too: it runs alongside the harvester to
                # stay under the spout while the grain flows.
                self._move(cart, productive=cart.id in unloading)

        for harvester in self.harvesters:
            harvester.waiting_ticks = (
                harvester.waiting_ticks + 1
                if harvester.state is HarvesterState.WAITING_CART
                else 0
            )

        idled = sum(agent.idle_ticks for agent in self.agents)
        self._idle_window.append(idled - self._idle_total)
        self._idle_total = idled

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
            stranded=sum(h.load for h in self.harvesters if h.disabled),
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
