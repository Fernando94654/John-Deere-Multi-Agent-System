"""Recommend a harvester / grain-cart fleet for a field and an operating budget.

The question an operator asks is "how many machines should I send, and can I
afford them?". Answering it by replaying the real simulation is too slow to sit
behind a request — one 50x50 run is ten seconds — so this uses a closed-form
estimate instead.

The estimate is a first-order model of what the engine does: harvesters cut one
cell per tick inside their zone, the zone split loses efficiency as it gets
finer, grain carts shuttle 60 units to the farm and back, and when the carts
cannot keep up the harvesters idle. Its constants were fitted against 72
recorded runs (12x14 up to 20x30, 1-4 harvesters, 1-3 carts); over that set it
lands within ~7% on duration and ~5% on fuel. It is meant for *ranking* fleets,
not for predicting a specific run to the tick, and it says so: every number it
returns is an estimate.

Nothing here touches a live `Session`; it only reads the engine's constants.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Mapping, Optional

from .config import (
    CART_CAPACITY,
    CO2_KG_PER_LITRE,
    FUEL_PER_CELL_HARVESTER,
    HARVESTER_TANK,
    REQUEST_THRESHOLD,
)

#: Crop a harvester must cut before it fills its tank far enough to call a cart.
#: Below this much reachable crop per harvester the carts would never leave the
#: farm, which is the same floor `server.py` enforces on a real run.
CROP_PER_HARVESTER = round(REQUEST_THRESHOLD * HARVESTER_TANK)

#: The engine caps a field at six grain carts before it gridlocks.
MAX_CARTS = 6

#: Widest fleet the advisor will weigh. Past this the marginal harvester never
#: wins a profile, and the candidate list stays cheap.
MAX_HARVESTERS = 16

#: A fleet is only "viable" for the frugal profiles if it finishes within this
#: multiple of the fastest affordable fleet's time.
VIABLE_SLOWDOWN = 1.6

# --- fitted model constants (see module docstring) --------------------------
_HARVEST_EFF = 0.565        # cells/tick a lone harvester clears, in its zone
_EFF_DECAY = 0.286          # how fast per-harvester efficiency falls as h grows
_TRAVEL_K = 0.252           # start-up + endgame ticks, per sqrt(area)
_CART_CYCLE_BASE = 25.08    # ticks of a cart round trip, fixed part
_CART_CYCLE_DIST = 6.20     # ... plus this per farm-to-cell distance
_FUEL_BASE = 10.44          # litres, fixed part
_FUEL_K = 1.416             # ... times the modelled litres of driving + idling


@dataclass(frozen=True)
class FleetCosts:
    """What a machine costs, and a label for the price list it came from."""

    currency: str
    cost_version: str
    harvester_cost: int
    cart_cost: int

    #: Environment variables that carry each field.
    ENV = {
        "currency": "FLEET_COST_CURRENCY",
        "cost_version": "FLEET_COST_VERSION",
        "harvester_cost": "FLEET_HARVESTER_COST",
        "cart_cost": "FLEET_CART_COST",
    }

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "FleetCosts":
        """Read the price list from the environment, or say exactly what is missing."""
        env = os.environ if env is None else env
        missing = [name for name in cls.ENV.values() if not env.get(name)]
        if missing:
            raise ValueError(
                "missing fleet cost configuration: " + ", ".join(sorted(missing))
            )
        try:
            costs = cls(
                currency=str(env[cls.ENV["currency"]]),
                cost_version=str(env[cls.ENV["cost_version"]]),
                harvester_cost=int(env[cls.ENV["harvester_cost"]]),
                cart_cost=int(env[cls.ENV["cart_cost"]]),
            )
        except (TypeError, ValueError):
            raise ValueError("fleet unit costs must be integers") from None
        if costs.harvester_cost < 0 or costs.cart_cost < 0:
            raise ValueError("fleet unit costs cannot be negative")
        return costs

    def price(self, harvesters: int, carts: int) -> int:
        return harvesters * self.harvester_cost + carts * self.cart_cost


@dataclass(frozen=True)
class Terrain:
    """A field, reduced to the handful of numbers the estimate needs."""

    rows: int
    cols: int
    border: int = 1
    min_obstacles: int = 3
    max_obstacles: int = 5
    food_ratio: float = 1.0

    def validated(self) -> "Terrain":
        if not isinstance(self.rows, int) or not isinstance(self.cols, int):
            raise ValueError("rows and columns must be integers")
        if self.rows < 3 or self.cols < 3:
            raise ValueError("the field needs to be at least 3x3")
        if self.border < 0:
            raise ValueError("the headland cannot be negative")
        if self.rows <= 2 * self.border or self.cols <= 2 * self.border:
            raise ValueError(
                f"a {self.rows}x{self.cols} field leaves nothing to sow inside a "
                f"{self.border}-cell headland"
            )
        if self.min_obstacles < 0 or self.max_obstacles < self.min_obstacles:
            raise ValueError("obstacle bounds must satisfy 0 <= min <= max")
        if not 0.0 <= self.food_ratio <= 1.0:
            raise ValueError("foodRatio must lie between 0 and 1")
        if self.inner_area <= self.max_obstacles:
            raise ValueError(
                f"a {self.rows}x{self.cols} field with a {self.border}-cell headland "
                f"has only {self.inner_area} workable cells, fewer than "
                f"{self.max_obstacles} obstacles"
            )
        return self

    @property
    def inner_area(self) -> int:
        """Cells that can hold crop: the field minus its headland."""
        return (self.rows - 2 * self.border) * (self.cols - 2 * self.border)

    @property
    def crop(self) -> float:
        """Expected reachable crop cells: sown ground minus the average rock draw."""
        avg_obstacles = (self.min_obstacles + self.max_obstacles) / 2
        return max(1.0, (self.inner_area - avg_obstacles) * self.food_ratio)

    @property
    def farm_distance(self) -> float:
        """Mean driving distance between the corner farm and a field cell."""
        return (self.rows + self.cols) / 3.0

    @property
    def max_harvesters(self) -> int:
        """Most harvesters this field can feed, by the call-a-cart floor."""
        by_crop = int(self.crop // CROP_PER_HARVESTER)
        return max(1, min(by_crop, MAX_HARVESTERS))


def estimate_run(terrain: Terrain, harvesters: int, carts: int) -> dict:
    """Estimate one campaign: how long it takes, and what it burns.

    Returns duration in ticks, fuel in litres, CO2 in kg, the share of the run
    the harvesters spend waiting on a cart, and which side is the bottleneck.
    """
    crop = terrain.crop
    area = terrain.rows * terrain.cols
    farm_distance = terrain.farm_distance

    # Harvesting throughput. A lone harvester clears ~_HARVEST_EFF cells/tick in
    # its zone; each extra harvester adds less, because the zones get finer and
    # share more edge.
    per_harvester = _HARVEST_EFF / (1 + _EFF_DECAY * (harvesters - 1))
    harvest_rate = harvesters * per_harvester

    # Cart throughput. One cart's round trip is a fixed cost plus travel; c carts
    # move c * 60 units per that cycle.
    cart_cycle = _CART_CYCLE_BASE + _CART_CYCLE_DIST * farm_distance
    cart_rate = carts * CART_CAPACITY / cart_cycle

    rate = min(harvest_rate, cart_rate)
    wait_rate = max(0.0, 1 - cart_rate / harvest_rate) if harvest_rate else 1.0
    bottleneck = "carts" if cart_rate < harvest_rate else "harvesting"

    travel = _TRAVEL_K * math.sqrt(area)
    duration = crop / rate + travel

    # Fuel. Harvesters drive every cut cell plus ~15% detours plus the run out to
    # their zone; carts drive a trip per 60 units delivered; idle engines burn a
    # trickle whenever a harvester is stalled waiting for a cart.
    harvester_cells = crop * 1.15 + harvesters * farm_distance
    cart_cells = (crop / CART_CAPACITY) * (2 * farm_distance + 0.5 * math.sqrt(area))
    idle_fuel = wait_rate * harvesters * duration * 0.05
    litres = (
        _FUEL_BASE
        + _FUEL_K * (harvester_cells * FUEL_PER_CELL_HARVESTER + cart_cells * 0.5 + idle_fuel)
    )

    return {
        "duration": round(duration),
        "fuel": round(litres, 1),
        "co2": round(litres * CO2_KG_PER_LITRE, 1),
        "deliveredUnits": round(crop),
        "harvesterWaitRate": round(wait_rate, 3),
        "bottleneck": bottleneck,
    }


def _candidates(terrain: Terrain, budget: int, costs: FleetCosts):
    """Every (harvesters, carts) pair that fits the field and the budget."""
    for harvesters in range(1, terrain.max_harvesters + 1):
        for carts in range(1, MAX_CARTS + 1):
            if costs.price(harvesters, carts) <= budget:
                yield harvesters, carts


def _profile(name: str, harvesters: int, carts: int, terrain: Terrain,
             budget: int, costs: FleetCosts) -> dict:
    cost = costs.price(harvesters, carts)
    return {
        "profile": name,
        "harvesters": harvesters,
        "carts": carts,
        "estimatedCost": cost,
        "budgetRemaining": budget - cost,
        "metrics": estimate_run(terrain, harvesters, carts),
    }


def recommend_fleet(
    rows: int,
    cols: int,
    *,
    budget: int,
    costs: FleetCosts,
    border: int = 1,
    min_obstacles: int = 3,
    max_obstacles: int = 5,
    food_ratio: float = 1.0,
) -> dict:
    """Pick standout fleets for this field within `budget`.

    Returns the fastest, the fewest machines, the most fuel-efficient, and the
    best overall value — each as a `{profile, harvesters, carts, estimatedCost,
    budgetRemaining, metrics}` row. Raises `ValueError` on an impossible field or
    a budget below the cheapest workable fleet.
    """
    terrain = Terrain(
        rows=rows, cols=cols, border=border,
        min_obstacles=min_obstacles, max_obstacles=max_obstacles,
        food_ratio=food_ratio,
    ).validated()

    if not isinstance(budget, int) or isinstance(budget, bool):
        raise ValueError("budget amount must be an integer")
    cheapest = costs.price(1, 1)
    if budget < cheapest:
        raise ValueError(
            f"budget {budget} {costs.currency} is below the cheapest workable "
            f"fleet, one harvester and one cart at {cheapest} {costs.currency}"
        )

    evaluated = [
        {"harvesters": h, "carts": c, **estimate_run(terrain, h, c)}
        for h, c in _candidates(terrain, budget, costs)
    ]
    # Ignore the single-cart traps unless one cart is genuinely all the field
    # needs (one harvester) — a lone cart is always the bottleneck otherwise.
    affordable = [e for e in evaluated if e["carts"] >= 2 or e["harvesters"] == 1] or evaluated

    fastest = min(e["duration"] for e in affordable)
    # "minimum machinery" and "lowest fuel" only get to choose among fleets that
    # still finish in a sane time — otherwise they always land on one harvester
    # crawling round the whole field.
    usable = [e for e in affordable if e["duration"] <= VIABLE_SLOWDOWN * fastest] or affordable

    durations = [e["duration"] for e in usable]
    fuels = [e["fuel"] for e in usable]
    costs_seen = [costs.price(e["harvesters"], e["carts"]) for e in usable]
    span = lambda xs: (max(xs) - min(xs)) or 1.0
    d_lo, d_span = min(durations), span(durations)
    f_lo, f_span = min(fuels), span(fuels)
    c_lo, c_span = min(costs_seen), span(costs_seen)

    def value(e: dict) -> float:
        cost = costs.price(e["harvesters"], e["carts"])
        return (
            0.55 * (e["duration"] - d_lo) / d_span
            + 0.30 * (cost - c_lo) / c_span
            + 0.15 * (e["fuel"] - f_lo) / f_span
        )

    picks = {
        "minimum_duration": min(affordable, key=lambda e: (e["duration"], e["fuel"])),
        "minimum_machinery": min(
            usable,
            key=lambda e: (e["harvesters"] + e["carts"],
                           costs.price(e["harvesters"], e["carts"]),
                           e["duration"]),
        ),
        "lower_consumption": min(usable, key=lambda e: (e["fuel"], e["duration"])),
        "balanced": min(usable, key=value),
    }

    return {
        "costVersion": costs.cost_version,
        "terrain": {
            "rows": terrain.rows,
            "columns": terrain.cols,
            "border": terrain.border,
            "minObstacles": terrain.min_obstacles,
            "maxObstacles": terrain.max_obstacles,
            "foodRatio": terrain.food_ratio,
        },
        "budget": {"amount": budget, "currency": costs.currency},
        "profiles": [
            _profile(name, e["harvesters"], e["carts"], terrain, budget, costs)
            for name, e in picks.items()
        ],
        "note": "estimates from a fitted model (~7% on duration, ~5% on fuel), not a simulated run",
    }
