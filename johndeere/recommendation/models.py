"""Structured inputs and outputs for fleet recommendation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class TerrainSpec:
    rows: int
    cols: int
    area: int
    aspect_ratio: float
    food_ratio: float = 1.0
    border: int = 1
    min_obstacles: int = 3
    max_obstacles: int = 5

    @classmethod
    def from_dimensions(
        cls,
        rows: int,
        cols: int,
        *,
        food_ratio: float = 1.0,
        border: int = 1,
        min_obstacles: int = 3,
        max_obstacles: int = 5,
    ) -> "TerrainSpec":
        if rows < 3 or cols < 3:
            raise ValueError("The field needs to be at least 3x3")
        if not 0.0 <= food_ratio <= 1.0:
            raise ValueError("food_ratio must lie between 0 and 1")
        if border < 0:
            raise ValueError("border cannot be negative")
        if min_obstacles < 0 or max_obstacles < min_obstacles:
            raise ValueError("obstacle bounds are invalid")
        return cls(
            rows,
            cols,
            rows * cols,
            max(rows, cols) / min(rows, cols),
            food_ratio,
            border,
            min_obstacles,
            max_obstacles,
        )


@dataclass(frozen=True)
class FleetCostConfig:
    currency: str
    cost_version: str
    harvester_cost: int
    cart_cost: int

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "FleetCostConfig":
        names = {
            "currency": "FLEET_COST_CURRENCY",
            "cost_version": "FLEET_COST_VERSION",
            "harvester_cost": "FLEET_HARVESTER_COST",
            "cart_cost": "FLEET_CART_COST",
        }
        missing = [env_name for env_name in names.values() if not values.get(env_name)]
        if missing:
            raise ValueError(
                "missing fleet cost configuration: " + ", ".join(missing)
            )
        try:
            config = cls(
                currency=values[names["currency"]],
                cost_version=values[names["cost_version"]],
                harvester_cost=int(values[names["harvester_cost"]]),
                cart_cost=int(values[names["cart_cost"]]),
            )
        except (TypeError, ValueError) as error:
            raise ValueError("fleet unit costs must be integers") from error
        if config.harvester_cost < 0 or config.cart_cost < 0:
            raise ValueError("fleet unit costs cannot be negative")
        return config

    def estimate(self, fleet: "FleetConfig") -> int:
        return fleet.harvesters * self.harvester_cost + fleet.carts * self.cart_cost


@dataclass(frozen=True, order=True)
class FleetConfig:
    harvesters: int
    carts: int

    @property
    def vehicles(self) -> int:
        return self.harvesters + self.carts

    @property
    def machinery_index(self) -> float:
        """Relative machinery quantity; deliberately not a monetary cost."""
        return self.harvesters + 0.6 * self.carts


@dataclass(frozen=True)
class RunMetrics:
    seed: int
    completed: bool
    duration_ticks: int
    fuel: float
    co2: float
    idle_ticks: int = 0
    cumulative_harvester_wait: int = 0
    max_harvester_wait: int = 0
    repeated_traffic: int = 0
    harvester_wait_rate: float = 0.0
    fuel_per_delivered_unit: float = 0.0
    traffic_per_delivered_unit: float = 0.0
    harvested: int = 0
    delivered: int = 0
    expected_reachable_crop: int = 0
    in_transit: int = 0
    stranded: int = 0
    collisions: int = 0
    obstacle_violations: int = 0
    error: Optional[str] = None


@dataclass
class FleetEvaluation:
    fleet: FleetConfig
    runs: list[RunMetrics] = field(default_factory=list)
    means: dict[str, float] = field(default_factory=dict)
    standard_deviations: dict[str, float] = field(default_factory=dict)
    completion_rate: float = 0.0
    eligible: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    normalized: dict[str, float] = field(default_factory=dict)
    score: Optional[float] = None
    beyond_marginal_point: bool = False
    is_pareto: bool = False


@dataclass(frozen=True)
class PairedComparison:
    origin: FleetConfig
    destination: FleetConfig
    seeds: int
    wins: int
    losses: int
    ties: int
    mean_tick_difference: float
    median_tick_difference: float
    mean_percent_change: float
    mean_wait_difference: float
    mean_fuel_difference: float
    mean_traffic_difference: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.seeds if self.seeds else 0.0


@dataclass
class FleetRecommendation:
    terrain: TerrainSpec
    seeds: tuple[int, ...]
    recommended: FleetEvaluation
    economical_alternative: FleetEvaluation
    maximum_productivity: FleetEvaluation
    evaluated_configurations: list[FleetEvaluation]
    reasons: list[str]
    sustainable_recommendation: Optional[FleetEvaluation] = None
    technical_tie: list[FleetEvaluation] = field(default_factory=list)
    confidence: str = "alta"
    stability_warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
