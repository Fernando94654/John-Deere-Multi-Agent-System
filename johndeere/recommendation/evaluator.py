"""Run fair, repeated simulations and assemble a fleet recommendation."""

from __future__ import annotations

import random
import statistics
from collections.abc import Iterable

from ..config import SimulationConfig
from ..simulation import Simulation
from ..world.grid import FOOD
from .candidates import generate_candidates, refinement_neighbors
from .models import (
    FleetConfig,
    FleetCostConfig,
    FleetEvaluation,
    FleetRecommendation,
    RunMetrics,
    TerrainSpec,
)
from .scoring import (
    assess_stability,
    build_reasons,
    choose,
    choose_sustainable,
    technical_finalists,
)

DEFAULT_BASE_SEED = 42
DEFAULT_SEED_COUNT = 7
MIN_SEEDS = 5
MAX_SEEDS = 30
METRIC_NAMES = (
    "duration_ticks",
    "fuel",
    "co2",
    "idle_ticks",
    "cumulative_harvester_wait",
    "harvester_wait_rate",
    "max_harvester_wait",
    "repeated_traffic",
    "fuel_per_delivered_unit",
    "traffic_per_delivered_unit",
    "harvested",
    "delivered",
)


def shared_seeds(
    base_seed: int = DEFAULT_BASE_SEED, count: int = DEFAULT_SEED_COUNT
) -> tuple[int, ...]:
    if not MIN_SEEDS <= count <= MAX_SEEDS:
        raise ValueError(f"seed count must be between {MIN_SEEDS} and {MAX_SEEDS}")
    return tuple(random.Random(base_seed).sample(range(2**31), count))


def run_campaign(terrain: TerrainSpec, fleet: FleetConfig, seed: int) -> RunMetrics:
    """Execute one heuristic campaign and return only scalar observations."""
    cumulative_wait = 0

    def observe(snapshot) -> None:
        nonlocal cumulative_wait
        cumulative_wait += sum(
            view.state == "waiting cart" for view in snapshot.harvesters
        )

    try:
        simulation = Simulation(
            SimulationConfig(
                rows=terrain.rows,
                cols=terrain.cols,
                harvesters=fleet.harvesters,
                carts=fleet.carts,
                food_ratio=terrain.food_ratio,
                border=terrain.border,
                min_obstacles=terrain.min_obstacles,
                max_obstacles=terrain.max_obstacles,
                seed=seed,
            )
        )
        result = simulation.run(on_tick=observe)
    except (RuntimeError, ValueError) as error:
        return RunMetrics(seed, False, 0, 0.0, 0.0, error=str(error))

    metrics = result.metrics
    initial_crop = sum(row.count(FOOD) for row in result.initial_grid)
    expected = initial_crop - result.unreachable_food
    return RunMetrics(
        seed=seed,
        completed=result.completed,
        duration_ticks=result.ticks,
        fuel=metrics.fuel,
        co2=metrics.co2,
        idle_ticks=metrics.idle_ticks,
        cumulative_harvester_wait=cumulative_wait,
        max_harvester_wait=metrics.max_harvester_wait_ticks,
        repeated_traffic=metrics.repeated_traffic,
        harvester_wait_rate=(
            cumulative_wait / (fleet.harvesters * result.ticks)
            if result.ticks
            else 0.0
        ),
        fuel_per_delivered_unit=(metrics.fuel / metrics.delivered if metrics.delivered else 0.0),
        traffic_per_delivered_unit=(
            metrics.repeated_traffic / metrics.delivered if metrics.delivered else 0.0
        ),
        harvested=metrics.harvested,
        delivered=metrics.delivered,
        expected_reachable_crop=expected,
        in_transit=metrics.in_transit,
        stranded=metrics.stranded,
        collisions=result.collisions,
        obstacle_violations=result.obstacle_violations,
    )


def aggregate(fleet: FleetConfig, runs: Iterable[RunMetrics]) -> FleetEvaluation:
    runs = list(runs)
    reasons: list[str] = []
    for run in runs:
        if run.error:
            reasons.append(f"seed {run.seed}: {run.error}")
        elif not run.completed:
            reasons.append(f"seed {run.seed}: campaign did not complete")
        elif run.harvested != run.expected_reachable_crop:
            reasons.append(f"seed {run.seed}: reachable crop was not fully harvested")
        elif run.delivered != run.expected_reachable_crop:
            reasons.append(f"seed {run.seed}: harvested grain was not fully delivered")
        elif run.in_transit or run.stranded:
            reasons.append(f"seed {run.seed}: grain remained outside the farm")
        elif run.collisions or run.obstacle_violations:
            reasons.append(f"seed {run.seed}: physical invariant violation")
    completion_rate = (
        sum(run.completed and not run.error for run in runs) / len(runs)
        if runs
        else 0.0
    )
    means = (
        {
            name: statistics.fmean(getattr(run, name) for run in runs)
            for name in METRIC_NAMES
        }
        if runs
        else {}
    )
    deviations = (
        {
            name: statistics.pstdev(getattr(run, name) for run in runs)
            for name in METRIC_NAMES
        }
        if runs
        else {}
    )
    return FleetEvaluation(
        fleet=fleet,
        runs=runs,
        means=means,
        standard_deviations=deviations,
        completion_rate=completion_rate,
        eligible=bool(runs) and not reasons and completion_rate == 1.0,
        rejection_reasons=reasons,
    )


def _evaluate(
    terrain: TerrainSpec, fleet: FleetConfig, seeds: tuple[int, ...]
) -> FleetEvaluation:
    return aggregate(
        fleet,
        (run_campaign(terrain, fleet, seed) for seed in seeds),
    )


def recommend_fleet(
    rows: int,
    cols: int,
    *,
    seed_count: int = DEFAULT_SEED_COUNT,
    base_seed: int = DEFAULT_BASE_SEED,
    food_ratio: float = 1.0,
    border: int = 1,
    min_obstacles: int = 3,
    max_obstacles: int = 5,
) -> FleetRecommendation:
    """Run a base search, then evaluate local neighbors of its provisional choice."""
    terrain = TerrainSpec.from_dimensions(
        rows,
        cols,
        food_ratio=food_ratio,
        border=border,
        min_obstacles=min_obstacles,
        max_obstacles=max_obstacles,
    )
    seeds = shared_seeds(base_seed, seed_count)
    base_candidates = generate_candidates(terrain)
    evaluations = [_evaluate(terrain, fleet, seeds) for fleet in base_candidates]
    provisional, _, _ = choose(evaluations)
    evaluated = {evaluation.fleet for evaluation in evaluations}
    neighbors = refinement_neighbors(provisional.fleet, evaluated)
    evaluations.extend(_evaluate(terrain, fleet, seeds) for fleet in neighbors)
    # A seed describes the terrain, so expected reachable yield must be fleet-independent.
    expected_by_seed: dict[int, set[int]] = {}
    for evaluation in evaluations:
        for run in evaluation.runs:
            if not run.error:
                expected_by_seed.setdefault(run.seed, set()).add(run.expected_reachable_crop)
    inconsistent = {seed for seed, values in expected_by_seed.items() if len(values) > 1}
    if inconsistent:
        for evaluation in evaluations:
            if any(run.seed in inconsistent for run in evaluation.runs):
                evaluation.eligible = False
                evaluation.rejection_reasons.append("terrain yield changed across fleets")

    recommended, economical, productive = choose(evaluations)
    sustainable = choose_sustainable(evaluations, recommended)
    finalists = technical_finalists(evaluations)
    confidence, warnings = assess_stability(evaluations, recommended)
    return FleetRecommendation(
        terrain=terrain,
        seeds=seeds,
        recommended=recommended,
        economical_alternative=economical,
        maximum_productivity=productive,
        evaluated_configurations=evaluations,
        reasons=build_reasons(
            recommended, sustainable, economical, productive, finalists, evaluations
        ),
        sustainable_recommendation=sustainable,
        technical_tie=finalists,
        confidence=confidence,
        stability_warnings=warnings,
    )


def recommendation_profiles(
    recommendation: FleetRecommendation,
    budget: int,
    costs: FleetCostConfig,
) -> list[dict]:
    """Return four structured, affordable profiles from evaluated campaigns."""
    if budget < 0:
        raise ValueError("budget amount cannot be negative")
    affordable = [
        item
        for item in recommendation.evaluated_configurations
        if item.eligible and costs.estimate(item.fleet) <= budget
    ]
    if not affordable:
        raise ValueError("budget is insufficient for every evaluated fleet")

    balanced = min(affordable, key=lambda item: (item.score, item.fleet.vehicles))
    lower_consumption = min(
        affordable,
        key=lambda item: (item.means["fuel"], item.means["duration_ticks"]),
    )
    minimum_machinery = min(
        affordable,
        key=lambda item: (item.fleet.vehicles, item.fleet.machinery_index, item.score),
    )
    minimum_duration = min(
        affordable,
        key=lambda item: (item.means["duration_ticks"], item.fleet.vehicles),
    )

    def payload(profile: str, item: FleetEvaluation) -> dict:
        estimated = costs.estimate(item.fleet)
        return {
            "profile": profile,
            "harvesters": item.fleet.harvesters,
            "carts": item.fleet.carts,
            "estimatedCost": estimated,
            "budgetRemaining": budget - estimated,
            "metrics": {
                "duration": round(item.means["duration_ticks"], 2),
                "fuel": round(item.means["fuel"], 2),
                "co2": round(item.means["co2"], 2),
                "repeatedTraffic": round(item.means["repeated_traffic"], 2),
                "harvesterWaitRate": round(item.means["harvester_wait_rate"], 4),
            },
        }

    return [
        payload("balanced", balanced),
        payload("lower_consumption", lower_consumption),
        payload("minimum_machinery", minimum_machinery),
        payload("minimum_duration", minimum_duration),
    ]


def recommend_fleet_profiles(
    rows: int,
    cols: int,
    *,
    budget: int,
    costs: FleetCostConfig,
    seed_count: int = DEFAULT_SEED_COUNT,
    base_seed: int = DEFAULT_BASE_SEED,
    food_ratio: float = 1.0,
    border: int = 1,
    min_obstacles: int = 3,
    max_obstacles: int = 5,
) -> dict:
    result = recommend_fleet(
        rows,
        cols,
        seed_count=seed_count,
        base_seed=base_seed,
        food_ratio=food_ratio,
        border=border,
        min_obstacles=min_obstacles,
        max_obstacles=max_obstacles,
    )
    return {
        "costVersion": costs.cost_version,
        "terrain": {
            "rows": result.terrain.rows,
            "columns": result.terrain.cols,
            "foodRatio": result.terrain.food_ratio,
            "border": result.terrain.border,
            "minObstacles": result.terrain.min_obstacles,
            "maxObstacles": result.terrain.max_obstacles,
        },
        "budget": {"amount": budget, "currency": costs.currency},
        "profiles": recommendation_profiles(result, budget, costs),
    }
