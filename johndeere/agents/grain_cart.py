"""The grain cart: the mobile container that empties harvesters.

A cart drives to the harvester that won it in the auction and then travels
**alongside its left-hand side**, where the unloading spout is, pulling grain
across while both machines keep moving. It heads for the farm when it fills up
or when there is nothing left to collect.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from ..config import CART_CAPACITY, FUEL_PER_CELL_CART, TRANSFER_RATE, UNLOAD_TICKS
from ..planning.pathfinding import a_star, bfs_distances
from ..world.field import Field
from ..world.grid import Cell
from .base import Agent
from .harvester import Harvester


class CartState(str, Enum):
    """Where a cart is in its ferrying cycle."""

    IDLE = "idle"
    TO_HARVESTER = "to harvester"
    TRANSFERRING = "transferring"
    TO_FARM = "to farm"
    UNLOADING = "unloading"


class GrainCart(Agent):
    """A mobile container ferrying grain from the harvesters to the farm."""

    prefix = "C"
    blocked_by_crop = True

    def __init__(self, agent_id: int, position: Cell):
        super().__init__(agent_id, position, FUEL_PER_CELL_CART)
        self.load = 0
        self.state = CartState.IDLE
        self.target_id: Optional[int] = None
        self.delivered = 0
        self.unload_countdown = 0

    @property
    def free_capacity(self) -> int:
        """Room left in the container."""
        return CART_CAPACITY - self.load

    @property
    def available(self) -> bool:
        """True when the cart can take on a new harvester.

        A cart on its way home with room to spare counts too: diverting it is
        cheaper than making a harvester wait for it to finish the round trip.
        """
        return (
            self.state in (CartState.IDLE, CartState.TO_FARM)
            and self.free_capacity > 0
        )

    def assign(self, harvester_id: int) -> None:
        """Send this cart to a harvester it just won in the auction."""
        self.target_id = harvester_id
        self.state = CartState.TO_HARVESTER

    def docked(self, harvester: Harvester) -> bool:
        """True when the cart sits exactly under the harvester's unloading spout.

        The spout is on the left-hand side, so being merely adjacent is not
        enough — driving behind the harvester would put the cart out of reach.
        """
        return self.position == harvester.left_cell

    def station(self, field: Field, harvester: Harvester) -> Optional[Cell]:
        """Where this cart should be to serve `harvester` right now.

        Normally the cell off the harvester's left side. When that one is a rock
        or lies outside the field, the cart escorts from any drivable cell beside
        the harvester and waits for the left side to open up again.
        """
        # Berths are ranked by how far the cart must actually drive to reach
        # them over cut ground, not by straight-line distance: the cell in front
        # of the harvester can be a stone's throw away and still have no route
        # to it, while the trail it just cut always leads somewhere.
        reach = bfs_distances(
            field, self.position, avoid_crop=True, blocked=(harvester.position,)
        )
        dock = harvester.left_cell
        if dock in reach or self.position == dock:
            return dock
        # The left-hand side is standing crop, a rock or off the field. Pull up
        # on any cut ground beside the harvester instead: it will turn on the
        # spot to bring its spout round to this side.
        berths = [
            cell
            for cell in field.neighbors(harvester.position, avoid_crop=True)
            if cell in reach
        ]
        if not berths:
            return None
        return min(berths, key=lambda cell: (reach[cell], cell))

    def keep_station(
        self, field: Field, harvester: Harvester, blocked: tuple[Cell, ...] = ()
    ) -> None:
        """Plan the move that keeps the cart in formation with `harvester`."""
        if self.docked(harvester):
            self.follow(None)
            return
        goal = self.station(field, harvester)
        if goal is None:
            self.follow(None)
        elif not self.route or self.route[-1] != goal:
            # Never route through the harvester itself: it is parked waiting for
            # this very cart, so that cell will not clear on its own.
            self.follow(
                a_star(
                    field,
                    self.position,
                    goal,
                    (*blocked, harvester.position),
                    avoid_crop=True,
                )
            )

    def decide(
        self,
        field: Field,
        target: Optional[Harvester],
        work_pending: bool,
        blocked: tuple[Cell, ...] = (),
    ) -> None:
        """Pick the state and the route for this tick."""
        if self.state is CartState.UNLOADING:
            return

        if self.state is CartState.TO_HARVESTER and target is not None:
            if self.docked(target):
                self.state = CartState.TRANSFERRING
                self.follow(None)
            else:
                # The harvester keeps cutting while it waits, so the docking
                # cell moves with it; re-plan towards wherever it is now.
                self.keep_station(field, target, blocked)
            return

        if self.state is CartState.TRANSFERRING:
            # Losing the dock for a tick — the harvester turned a corner, or the
            # left side ran into a rock — does not break the pairing: the cart
            # stays coupled and keeps station until the tank is empty or it is
            # full itself.
            if target is None or target.load == 0 or self.free_capacity == 0:
                self.release()
            else:
                self.keep_station(field, target, blocked)
            return

        if self.state is CartState.TO_FARM:
            if self.position == field.farm:
                self.arrive_home()
            elif not self.route or self.route[-1] != field.farm:
                self.follow(
                    a_star(field, self.position, field.farm, blocked, avoid_crop=True)
                )
            return

        # Idle. A cart parked in the middle of the field is a rock in everyone
        # else's way — and it may be sitting on crop nobody can then reach — so
        # an unassigned cart always drives home, to unload or just to park.
        if self.position == field.farm:
            self.arrive_home()
            return
        self.state = CartState.TO_FARM
        self.follow(a_star(field, self.position, field.farm, blocked, avoid_crop=True))

    def arrive_home(self) -> None:
        """Park at the farm: start unloading if loaded, otherwise wait for work."""
        self.follow(None)
        if self.load > 0:
            self.state = CartState.UNLOADING
            self.unload_countdown = UNLOAD_TICKS
        else:
            self.state = CartState.IDLE

    def release(self) -> None:
        """Stop serving the current harvester."""
        self.target_id = None
        self.state = CartState.IDLE
        self.follow(None)

    def pull_grain(self, harvester: Harvester) -> int:
        """Move up to `TRANSFER_RATE` units out of `harvester` into the cart."""
        moved = harvester.receive_from_tank(min(TRANSFER_RATE, self.free_capacity))
        self.load += moved
        return moved

    def unload(self) -> int:
        """Spend a tick emptying into the farm; returns units delivered."""
        self.unload_countdown -= 1
        if self.unload_countdown > 0:
            return 0
        delivered = self.load
        self.load = 0
        self.delivered += delivered
        self.state = CartState.IDLE
        return delivered
