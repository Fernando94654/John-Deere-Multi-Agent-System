"""The field map and how it is generated.

This module owns the grid vocabulary — the cell values and the `Grid` type — and
builds maps that honour the field constraints. It prints nothing and depends on
no external libraries.

Grid values:
    -1  obstacle (impassable: machines must drive around it)
     0  empty ground with no crop
     1  ground with crop (harvesting it sets the cell to 0 and yields one unit)
"""

from __future__ import annotations

import random
from typing import Optional

Cell = tuple[int, int]
Grid = list[list[int]]

OBSTACLE = -1
EMPTY = 0
FOOD = 1


def generate_grid(
    rows: int,
    cols: int,
    food_ratio: float = 1.0,
    min_obstacles: int = 3,
    max_obstacles: int = 5,
    seed: Optional[int] = None,
    farm: Optional[Cell] = None,
    border: int = 1,
) -> Grid:
    """Generate a `rows` x `cols` map holding the values -1, 0 and 1.

    The map honours two hard constraints instead of per-cell probabilities:
    the number of obstacles is drawn from `[min_obstacles, max_obstacles]`, and
    `food_ratio` is the share of the **sowable** ground that holds crop — the
    field minus its obstacles and minus the farmyard. So `food_ratio=1` sows
    every cell that is not a rock or the farm, whatever the obstacle draw turns
    out to be.

    `border` leaves a bare headland of that many cells around the edge of the
    field: no crop and no rocks, so there is a ring road the machines — grain
    carts above all, which cannot drive on standing crop — can use from the
    first tick instead of waiting for a harvester to cut them a way through.

    `farm` marks a cell that must stay clear: the farmyard never holds crop and
    never holds an obstacle, so machines can always enter and leave it.

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

    if border < 0:
        raise ValueError("border cannot be negative")
    if rows <= 2 * border or cols <= 2 * border:
        raise ValueError(
            f"A {rows}x{cols} field leaves nothing to sow inside a "
            f"{border}-cell headland"
        )

    inner_cells = (rows - 2 * border) * (cols - 2 * border)
    if max_obstacles > inner_cells:
        raise ValueError(
            f"A {rows}x{cols} field with a {border}-cell headland has only "
            f"{inner_cells} cells to work with, not enough for up to "
            f"{max_obstacles} obstacles"
        )

    rng = random.Random(seed)
    n_obstacles = rng.randint(min_obstacles, max_obstacles)
    grid: Grid = [[EMPTY] * cols for _ in range(rows)]

    # Only the ground inside the headland is sown or seeded with rocks.
    candidates = [
        row * cols + col
        for row in range(border, rows - border)
        for col in range(border, cols - border)
    ]
    if farm is not None:
        if not (0 <= farm[0] < rows and 0 <= farm[1] < cols):
            raise ValueError(f"The farm at {farm} falls outside a {rows}x{cols} grid")
        farm_index = farm[0] * cols + farm[1]
        if farm_index in candidates:
            candidates.remove(farm_index)

    # The ratio is measured against what is left once the headland, the rocks
    # and the farmyard are out of the way, so the caller does not have to work
    # around a draw it cannot see.
    sowable = len(candidates) - n_obstacles
    food_count = round(food_ratio * sowable)
    positions = rng.sample(candidates, n_obstacles + food_count)
    for index in positions[:n_obstacles]:
        grid[index // cols][index % cols] = OBSTACLE
    for index in positions[n_obstacles:]:
        grid[index // cols][index % cols] = FOOD
    return grid
