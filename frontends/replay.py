"""Replaying a finished run frame by frame.

The engine harvests the field in place, so by the time a run ends the grid is
bare. Both frontends therefore replay: they start from the map as it was and
apply each tick's harvested cells, which reproduces exactly what happened.
"""

from __future__ import annotations

from typing import Iterator

from johndeere.simulation import SimulationResult, Snapshot
from johndeere.world.grid import EMPTY, Grid


def frames(result: SimulationResult) -> Iterator[tuple[Grid, Snapshot]]:
    """Yield the field state and the snapshot for every tick of the run.

    The same grid object is handed out each time — a frontend that needs to keep
    a frame around must copy it.
    """
    grid = [row[:] for row in result.initial_grid]
    for snapshot in result.snapshots:
        for row, col in snapshot.harvested_cells:
            grid[row][col] = EMPTY
        yield grid, snapshot
