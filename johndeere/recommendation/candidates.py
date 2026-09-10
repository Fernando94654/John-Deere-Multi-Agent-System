"""Generate a small, shape-aware set of plausible fleets."""

from __future__ import annotations

import math

from .models import FleetConfig, TerrainSpec

TARGET_CROP_CELLS_PER_HARVESTER = 80
MIN_LANE_WIDTH = 2
ABSOLUTE_MAX_HARVESTERS = 5
ABSOLUTE_MAX_CARTS = 4
MAX_CONFIGURATIONS = 10
MAX_TOTAL_CONFIGURATIONS = 15


def fleet_limits(terrain: TerrainSpec) -> tuple[int, int]:
    """Return conservative harvester/cart limits for this field geometry."""
    inner_rows = terrain.rows - 2 * terrain.border
    inner_cols = terrain.cols - 2 * terrain.border
    if inner_rows <= 0 or inner_cols <= 0:
        return 1, 1
    estimated_crop = max(
        1,
        int(inner_rows * inner_cols * terrain.food_ratio) - terrain.max_obstacles,
    )
    # Elongation starts reducing useful parallelism only beyond a 2:1 field.
    shape_penalty = math.sqrt(max(1.0, terrain.aspect_ratio / 2.0))
    area_limit = math.ceil(
        estimated_crop / (TARGET_CROP_CELLS_PER_HARVESTER * shape_penalty)
    )
    lane_limit = max(1, min(inner_rows, inner_cols) // MIN_LANE_WIDTH)
    harvesters = max(
        1, min(area_limit, lane_limit, ABSOLUTE_MAX_HARVESTERS)
    )
    return harvesters, min(harvesters, ABSOLUTE_MAX_CARTS)


def generate_candidates(
    terrain: TerrainSpec, limit: int = MAX_CONFIGURATIONS
) -> list[FleetConfig]:
    """Return deterministic candidates, retaining balanced and boundary fleets."""
    if limit < 1:
        raise ValueError("candidate limit must be positive")
    max_harvesters, max_carts = fleet_limits(terrain)
    possible = {
        FleetConfig(h, c)
        for h in range(1, max_harvesters + 1)
        for c in range(1, min(h, max_carts) + 1)
    }
    if len(possible) <= limit:
        return sorted(possible, key=lambda f: (f.vehicles, f.harvesters, f.carts))

    # Preserve the baseline, a balanced fleet and the cart-rich boundary at
    # every scale before filling remaining slots with small adjacent upgrades.
    priority: list[FleetConfig] = [FleetConfig(1, 1)]
    for h in range(2, max_harvesters + 1):
        priority.extend(
            (
                FleetConfig(h, min(max_carts, math.ceil(h / 2))),
                FleetConfig(h, min(max_carts, h)),
            )
        )
    priority.extend(
        sorted(
            possible,
            key=lambda f: (f.vehicles, abs(f.carts - f.harvesters / 2), f),
        )
    )
    selected: list[FleetConfig] = []
    for fleet in priority:
        if fleet in possible and fleet not in selected:
            selected.append(fleet)
        if len(selected) == limit:
            break
    return sorted(selected, key=lambda f: (f.vehicles, f.harvesters, f.carts))


def refinement_neighbors(
    provisional: FleetConfig,
    evaluated: set[FleetConfig],
    total_limit: int = MAX_TOTAL_CONFIGURATIONS,
) -> list[FleetConfig]:
    """Return only unexplored local boundary configurations around a choice."""
    if total_limit < len(evaluated):
        return []
    h, c = provisional.harvesters, provisional.carts
    candidates = (
        FleetConfig(h + 1, c),
        FleetConfig(h + 1, c + 1),
        FleetConfig(h, c + 1),  # the single cart-rich boundary for this H
        FleetConfig(h - 1, c),
        FleetConfig(h, c - 1),
    )
    remaining = total_limit - len(evaluated)
    return [
        fleet
        for fleet in dict.fromkeys(candidates)
        if fleet.harvesters > 0
        and fleet.carts > 0
        and fleet not in evaluated
    ][:remaining]
