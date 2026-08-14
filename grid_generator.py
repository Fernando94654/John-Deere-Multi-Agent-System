"""Generation of the field the tractors harvest.

This module owns the grid vocabulary — the cell values and the `Grid` type —
and builds maps that honour the field constraints. It prints nothing and
depends on no external libraries.

Grid values:
    -1  obstacle (a tractor may drive over it, but there is nothing to harvest)
     0  empty ground with no food
     1  ground with food (driving over it sets the cell to 0 and adds to the counter)
"""

from __future__ import annotations

import math
import random
from typing import Optional

Grid = list[list[int]]

OBSTACLE = -1
EMPTY = 0
FOOD = 1


def generate_grid(
    rows: int,
    cols: int,
    food_ratio: float = 0.8,
    min_obstacles: int = 3,
    max_obstacles: int = 5,
    seed: Optional[int] = None,
) -> Grid:
    """Generate a `rows` x `cols` map holding the values -1, 0 and 1.

    The map honours two hard constraints instead of per-cell probabilities:
    the number of obstacles is drawn from `[min_obstacles, max_obstacles]`, and
    exactly `round(food_ratio * rows * cols)` cells hold food. Obstacles are
    placed first and food is sampled from the cells left over, so the two never
    collide and the food share is exact against the whole grid.

    Passing `seed` makes the map reproducible.
    """
    if rows < 1 or cols < 1:
        raise ValueError("The grid needs at least one row and one column")
    if not 0 <= food_ratio <= 1:
        raise ValueError("food_ratio must be between 0 and 1")
    if min_obstacles < 1 or max_obstacles < min_obstacles:
        raise ValueError(
            "Obstacle bounds must satisfy 1 <= min_obstacles <= max_obstacles"
        )

    total = rows * cols
    food_count = round(food_ratio * total)

    # Checked against max_obstacles rather than the drawn count so that a given
    # set of settings either always works or always fails, never at random.
    if food_count + max_obstacles > total:
        # The epsilon absorbs the float error of ratios such as 0.8, where
        # 5 / (1 - 0.8) evaluates to 25.000000000000004 instead of 25.
        hint = (
            f" These settings need a grid of at least "
            f"{math.ceil(max_obstacles / (1 - food_ratio) - 1e-9)} cells."
            if food_ratio < 1
            else ""
        )
        raise ValueError(
            f"A {rows}x{cols} grid has {total} cells, not enough for "
            f"{food_count} food cells plus up to {max_obstacles} obstacles.{hint}"
        )

    rng = random.Random(seed)
    n_obstacles = rng.randint(min_obstacles, max_obstacles)
    grid: Grid = [[EMPTY] * cols for _ in range(rows)]
    positions = rng.sample(range(total), n_obstacles + food_count)
    for index in positions[:n_obstacles]:
        grid[index // cols][index % cols] = OBSTACLE
    for index in positions[n_obstacles:]:
        grid[index // cols][index % cols] = FOOD
    return grid
