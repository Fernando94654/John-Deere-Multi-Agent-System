"""Sweep logic for a fleet of harvesting tractors over a grid.

This module holds the algorithm only: it prints nothing and depends on no
external libraries. The field itself is built by `grid_generator.py` and the
presentation lives in `cli_simulation.py`.

The grid is split into contiguous bands, one per tractor, and every tractor
enters the field from a border cell.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from grid_generator import EMPTY, FOOD, Grid

Cell = tuple[int, int]


def split_units(count: int, parts: int) -> list[tuple[int, int]]:
    """Split `count` consecutive units into `parts` contiguous inclusive ranges.

    The first `count % parts` ranges get one extra unit, so widths differ by at
    most one unit.
    """
    base, remainder = divmod(count, parts)
    ranges: list[tuple[int, int]] = []
    start = 0
    for i in range(parts):
        width = base + (1 if i < remainder else 0)
        ranges.append((start, start + width - 1))
        start += width
    return ranges


def column_band_path(rows: int, first_col: int, last_col: int) -> list[Cell]:
    """Serpentine sweep of a vertical band, entering at the top border.

    The band starts at `(0, first_col)` — a cell on the top edge of the grid —
    goes down the first column, up the next one, and so on.
    """
    path: list[Cell] = []
    for offset, col in enumerate(range(first_col, last_col + 1)):
        rows_range = range(rows) if offset % 2 == 0 else reversed(range(rows))
        for row in rows_range:
            path.append((row, col))
    return path


def row_band_path(cols: int, first_row: int, last_row: int) -> list[Cell]:
    """Serpentine sweep of a horizontal band, entering at the left border.

    The band starts at `(first_row, 0)` — a cell on the left edge of the grid —
    goes right along the first row, left along the next one, and so on.
    """
    path: list[Cell] = []
    for offset, row in enumerate(range(first_row, last_row + 1)):
        cols_range = range(cols) if offset % 2 == 0 else reversed(range(cols))
        for col in cols_range:
            path.append((row, col))
    return path


def plan_paths(rows: int, cols: int, n_tractors: int) -> list[list[Cell]]:
    """Assign one contiguous band of the grid to each tractor.

    Every tractor must enter the field from an edge, so the grid is cut into
    whole bands: a tractor sweeping a band enters its first column (or row) at
    a border cell and can only step into the next one at a border row (or
    column). That rules out cutting mid-column, which is why areas can differ
    by a whole band instead of a single cell.

    To keep that difference as small as possible the cut is made along the
    longer side of the grid, so two areas never differ by more than
    `min(rows, cols)` cells:

    - `cols >= rows` -> vertical bands, each tractor starts on the top edge.
    - `rows > cols`  -> horizontal bands, each tractor starts on the left edge.
    """
    if n_tractors < 1:
        raise ValueError("At least one tractor is required")

    vertical = cols >= rows
    available = cols if vertical else rows
    if n_tractors > available:
        side = "columns" if vertical else "rows"
        raise ValueError(
            f"Cannot give {n_tractors} tractors a band each on a {rows}x{cols} "
            f"grid: it only has {available} {side} to split, and every tractor "
            "must start on a border"
        )

    bands = split_units(available, n_tractors)
    if vertical:
        return [column_band_path(rows, first, last) for first, last in bands]
    return [row_band_path(cols, first, last) for first, last in bands]


@dataclass
class Tractor:
    """A harvesting tractor with its assigned chunk of the sweep."""

    id: int
    path: list[Cell] = field(repr=False)
    step: int = 0
    food: int = 0
    position: Optional[Cell] = None

    @property
    def area(self) -> int:
        """Number of cells assigned to this tractor."""
        return len(self.path)

    @property
    def row_span(self) -> tuple[int, int]:
        """First and last row this tractor covers, both inclusive."""
        rows = [row for row, _ in self.path]
        return min(rows), max(rows)

    @property
    def column_span(self) -> tuple[int, int]:
        """First and last column this tractor covers, both inclusive."""
        columns = [col for _, col in self.path]
        return min(columns), max(columns)

    @property
    def start_cell(self) -> Cell:
        """The border cell this tractor enters the field from."""
        return self.path[0]

    @property
    def done(self) -> bool:
        """True once the whole path has been walked."""
        return self.step >= len(self.path)


@dataclass
class SimulationResult:
    """Summary of a full run."""

    tractors: list[Tractor]
    total_food: int
    turns: int


def simulate(
    grid: Grid,
    n_tractors: int,
    on_turn_end: Optional[Callable[[int, list[Tractor]], None]] = None,
) -> SimulationResult:
    """Sweep the grid with `n_tractors` advancing one step per turn.

    The `grid` is modified in place: every food cell a tractor drives over is
    set to 0. Cells holding -1 and 0 are left untouched.

    `on_turn_end(turn, tractors)` is an optional hook so a visualization can
    observe the run without the algorithm depending on it.
    """
    rows = len(grid)
    cols = len(grid[0])
    tractors = [
        Tractor(id=i, path=path)
        for i, path in enumerate(plan_paths(rows, cols, n_tractors))
    ]

    total_food = 0
    turns = 0
    while any(not tractor.done for tractor in tractors):
        for tractor in tractors:
            if tractor.done:
                continue
            row, col = tractor.path[tractor.step]
            tractor.position = (row, col)
            tractor.step += 1
            if grid[row][col] == FOOD:
                grid[row][col] = EMPTY
                tractor.food += 1
                total_food += 1
        turns += 1
        if on_turn_end is not None:
            on_turn_end(turns, tractors)

    return SimulationResult(tractors=tractors, total_food=total_food, turns=turns)
