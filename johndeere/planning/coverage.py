"""The order in which a harvester works through the cells of its zone.

The zone says *what* a harvester owns; this module says *in what order* it
drives through it. Keeping the classic serpentine order is what makes the sweep
read as deliberate on screen — the detours around obstacles are then left to
`a_star`, which joins one target to the next.
"""

from __future__ import annotations

from typing import Iterable

from ..world.field import Field
from ..world.grid import Cell


def serpentine_order(cells: Iterable[Cell]) -> list[Cell]:
    """Order `cells` as a boustrophedon sweep: down one column, up the next.

    Cells missing from a column (obstacles, or cells owned by another zone)
    simply leave a gap; the mover bridges it with a route around the obstacle.
    """
    by_column: dict[int, list[Cell]] = {}
    for cell in cells:
        by_column.setdefault(cell[1], []).append(cell)

    ordered: list[Cell] = []
    for index, column in enumerate(sorted(by_column)):
        column_cells = sorted(by_column[column], reverse=index % 2 == 1)
        ordered.extend(column_cells)
    return ordered


def zone_work_plan(field: Field, zone: Iterable[Cell]) -> list[Cell]:
    """The crop cells of `zone`, in the order a harvester should visit them."""
    return serpentine_order(cell for cell in zone if field.has_food(cell))
