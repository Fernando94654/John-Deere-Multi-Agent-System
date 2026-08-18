"""Every tunable number of the simulation, in one place.

Machine capacities are hardcoded on purpose: they are picked so the grain
logistics are visible within a short run (a harvester fills up every ~20 cells,
a cart serves three tank-fulls before it has to drive back to the farm).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# --- machine specs (hardcoded) ------------------------------------------------
HARVESTER_TANK = 20  # grain units a harvester can hold
CART_CAPACITY = 60  # grain units a cart can hold: three harvester tanks
TRANSFER_RATE = 2  # grain units moved per tick while unloading into a cart
REQUEST_THRESHOLD = 0.5  # a harvester calls for a cart at this share of its tank
UNLOAD_TICKS = 3  # ticks a cart spends emptying itself at the farm

# --- sustainability model -----------------------------------------------------
FUEL_PER_CELL_HARVESTER = 0.8  # litres burned per cell driven
FUEL_PER_CELL_CART = 0.5
FUEL_PER_IDLE_TICK = 0.05  # engines keep running while waiting
CO2_KG_PER_LITRE = 2.68  # diesel emission factor

# --- coordination -------------------------------------------------------------
BLOCKED_TICKS_BEFORE_REROUTE = 3  # patience before planning around a blocker
WAIT_WEIGHT = 1.5  # how much a harvester's wait discounts an auction bid


@dataclass
class SimulationConfig:
    """Parameters of a run. Everything not listed here is hardcoded above."""

    rows: int = 16
    cols: int = 22
    harvesters: int = 3
    carts: int = 2
    food_ratio: float = 1.0  # share of the sowable ground, headland and rocks aside
    border: int = 1  # bare headland around the field, for the machines to drive on
    min_obstacles: int = 3
    max_obstacles: int = 5
    seed: Optional[int] = None
    max_ticks: int = 5000

    def validate(self) -> None:
        """Raise `ValueError` if the fleet or the field make no sense."""
        if self.harvesters < 1:
            raise ValueError("At least one harvester is required")
        if self.carts < 1:
            raise ValueError("At least one grain cart is required")
        if self.rows < 3 or self.cols < 3:
            raise ValueError("The field needs to be at least 3x3")
        if self.border < 0:
            raise ValueError("The headland cannot be negative")
