"""The harvester: sweeps its zone, fills its tank, calls for a grain cart.

A harvester never drives its grain to the farm itself — that is the cart's job.
It calls for one at `REQUEST_THRESHOLD` and keeps working while it waits.

Grain leaves over the left-hand side. A cart cannot drive on standing crop, so
more often than not the only cut ground beside the harvester is somewhere other
than its left. In that case the harvester stops, turns on the spot until the
cart is on its left, empties, and carries on.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from ..config import (
    FUEL_PER_CELL_HARVESTER,
    HARVESTER_TANK,
    REQUEST_THRESHOLD,
)
from ..planning.pathfinding import a_star
from ..world.field import Field, heading_for_dock
from ..world.grid import Cell
from .base import Agent


class HarvesterState(str, Enum):
    """Where a harvester is in its working day."""

    TO_ZONE = "to zone"
    HARVESTING = "harvesting"
    ROTATING = "rotating"
    UNLOADING = "unloading"
    WAITING_CART = "waiting cart"
    RETURNING = "returning"
    DONE = "done"


class Harvester(Agent):
    """A combine working one zone of the field."""

    prefix = "H"

    def __init__(self, agent_id: int, position: Cell, zone: set[Cell], plan: list[Cell]):
        super().__init__(agent_id, position, FUEL_PER_CELL_HARVESTER)
        self.zone = zone
        self.plan = list(plan)
        self.load = 0
        self.state = HarvesterState.TO_ZONE
        self.harvested = 0
        self.requested = False
        self.cart_id: Optional[int] = None
        self.unloading = False

    # --- state queries ----------------------------------------------------
    @property
    def full(self) -> bool:
        """True when there is no room left in the tank."""
        return self.load >= HARVESTER_TANK

    @property
    def wants_cart(self) -> bool:
        """True once the tank crosses the threshold that calls for a cart."""
        return self.load >= REQUEST_THRESHOLD * HARVESTER_TANK

    @property
    def done(self) -> bool:
        return self.state is HarvesterState.DONE

    def receive_from_tank(self, units: int) -> int:
        """Hand `units` of grain to a cart; returns what was actually taken."""
        moved = min(units, self.load)
        self.load -= moved
        return moved

    # --- behaviour --------------------------------------------------------
    def next_target(self, field: Field) -> Optional[Cell]:
        """The next crop cell of the plan that still has something to cut."""
        while self.plan and not field.has_food(self.plan[0]):
            self.plan.pop(0)
        return self.plan[0] if self.plan else None

    def needs_unloading(self, field: Field) -> bool:
        """True when this machine cannot go on until a cart takes its grain."""
        return self.load > 0 and (self.full or self.next_target(field) is None)

    def decide(
        self,
        field: Field,
        cart_cell: Optional[Cell] = None,
        blocked: tuple[Cell, ...] = (),
    ) -> None:
        """Pick the state and the route for this tick.

        `cart_cell` is where this harvester's assigned cart is standing, when it
        has pulled up alongside.
        """
        if self.state is HarvesterState.DONE:
            return

        if self.load == 0 or cart_cell is None:
            self.unloading = False
        elif cart_cell != self.left_cell:
            # The cart could only find cut ground on another side: stop, turn to
            # face it, and stay put until the tank is empty. Without the latch
            # the machine would drive off again the moment the turn lined up.
            self.unloading = True

        if self.unloading and cart_cell is not None:
            self.follow(None)
            wanted = heading_for_dock(self.position, cart_cell)
            if self.heading == wanted:
                self.state = HarvesterState.UNLOADING
            else:
                # One quarter turn per tick, and no grain moves while turning:
                # a cart that arrives on the wrong side costs real time.
                self.turn_towards(wanted)
                self.state = HarvesterState.ROTATING
            return

        # A full tank — or a finished zone with grain still aboard — stops the
        # machine until a cart reaches it.
        if self.needs_unloading(field):
            self.state = HarvesterState.WAITING_CART
            self.follow(None)
            return

        target = self.next_target(field)
        if target is None:
            # Nothing left to cut and nothing left to hand over: drive home.
            self.state = HarvesterState.RETURNING
            if self.position == field.farm:
                self.state = HarvesterState.DONE
                self.follow(None)
            elif not self.route or self.route[-1] != field.farm:
                self.follow(a_star(field, self.position, field.farm, blocked))
            return

        self.state = (
            HarvesterState.HARVESTING
            if self.position in self.zone
            else HarvesterState.TO_ZONE
        )
        if not self.route or self.route[-1] != target:
            self.follow(a_star(field, self.position, target, blocked))

    def defer_target(self) -> None:
        """Send the current target to the back of the plan.

        Used when a machine is parked exactly on the next cell to cut: no detour
        exists to a cell that is itself the obstacle, so the harvester works
        elsewhere and comes back to it later.
        """
        if self.plan:
            self.plan.append(self.plan.pop(0))
        self.follow(None)

    def work(self, field: Field) -> Optional[Cell]:
        """Cut the crop under the machine; returns the cell if it yielded grain."""
        if self.full or not field.has_food(self.position):
            return None
        taken = field.harvest(self.position)
        self.load += taken
        self.harvested += taken
        return self.position if taken else None
