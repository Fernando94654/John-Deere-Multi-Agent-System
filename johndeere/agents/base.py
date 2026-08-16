"""What every machine in the fleet has in common.

`Agent` holds the physical part — where it stands, the route it is following,
and the fuel and time it burns. The decision making lives in the subclasses.
"""

from __future__ import annotations

from typing import Optional

from ..config import CO2_KG_PER_LITRE, FUEL_PER_IDLE_TICK
from ..world.field import EAST, left_cell, left_of, right_of
from ..world.grid import Cell


class Agent:
    """A machine that drives one cell per tick along a planned route."""

    #: Prefix used to label the agent in the frontends, e.g. "H0", "C1".
    prefix = "A"

    #: Whether standing crop stops this machine. Carts would flatten it.
    blocked_by_crop = False

    def __init__(self, agent_id: int, position: Cell, fuel_per_cell: float):
        self.id = agent_id
        self.position = position
        self.fuel_per_cell = fuel_per_cell
        self.route: list[Cell] = []
        self.heading: tuple[int, int] = EAST
        self.distance = 0
        self.fuel = 0.0
        self.idle_ticks = 0
        self.blocked_ticks = 0

    def __repr__(self) -> str:
        return f"{self.label}@{self.position}"

    @property
    def label(self) -> str:
        """Short identifier shown on screen."""
        return f"{self.prefix}{self.id}"

    @property
    def co2(self) -> float:
        """Kilograms of CO2 emitted so far."""
        return self.fuel * CO2_KG_PER_LITRE

    @property
    def next_cell(self) -> Optional[Cell]:
        """The cell this agent wants to enter next, if it is following a route."""
        return self.route[0] if self.route else None

    def follow(self, route: Optional[list[Cell]]) -> None:
        """Adopt `route` as the plan; `None` clears it."""
        self.route = list(route) if route else []

    @property
    def left_cell(self) -> Cell:
        """The cell alongside this machine's left-hand side."""
        return left_cell(self.position, self.heading)

    def advance(self) -> None:
        """Take the next step of the route, paying its fuel."""
        step = self.route.pop(0)
        self.heading = (step[0] - self.position[0], step[1] - self.position[1])
        self.position = step
        self.distance += 1
        self.fuel += self.fuel_per_cell
        self.blocked_ticks = 0

    def turn_towards(self, heading: tuple[int, int]) -> bool:
        """Rotate a quarter turn towards `heading`; True once facing it.

        A half turn therefore takes two ticks, which is what makes a cart
        arriving on the wrong side cost something visible.
        """
        if self.heading == heading:
            return True
        self.heading = (
            left_of(self.heading)
            if left_of(self.heading) == heading
            or left_of(left_of(self.heading)) == heading
            else right_of(self.heading)
        )
        return self.heading == heading

    def hold(self, productive: bool = False) -> None:
        """Stay put for this tick, idling the engine.

        `productive` marks a stop that is doing work — a cart parked alongside a
        harvester is moving grain, not waiting — so it burns idle fuel without
        counting as idle time, the metric used to size the fleet.
        """
        if not productive:
            self.idle_ticks += 1
        self.fuel += FUEL_PER_IDLE_TICK
