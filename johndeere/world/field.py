"""The world model the agents query and mutate.

`Field` wraps the raw grid with the rules of the terrain. The important one:
obstacles are **impassable**. Everything downstream — pathfinding, the zone
partition, the coverage order — is built on `passable()` and `neighbors()`.
"""

from __future__ import annotations

from .grid import EMPTY, FOOD, OBSTACLE, Cell, Grid

# Machines drive along the field rows and columns, never diagonally.
STEPS = ((-1, 0), (1, 0), (0, -1), (0, 1))

EAST = (0, 1)


def left_of(heading: tuple[int, int]) -> tuple[int, int]:
    """The step that points to the left-hand side of a machine facing `heading`.

    Rows grow downwards on screen, so turning left is `(dr, dc) -> (-dc, dr)`:
    facing north the left is west, facing east the left is north, and so on.
    """
    dr, dc = heading
    return (-dc, dr)


def right_of(heading: tuple[int, int]) -> tuple[int, int]:
    """The step that points to the right-hand side of a machine facing `heading`."""
    dr, dc = heading
    return (dc, -dr)


def heading_for_dock(position: Cell, dock: Cell) -> tuple[int, int]:
    """The heading that leaves `dock` on the left-hand side of `position`.

    Inverting `left_of`: to get `left_of(h) == d` the machine must face
    `(d_c, -d_r)`, which is `d` turned to the right.
    """
    return right_of((dock[0] - position[0], dock[1] - position[1]))


def left_cell(position: Cell, heading: tuple[int, int]) -> Cell:
    """The cell alongside the left-hand side of a machine at `position`."""
    dr, dc = left_of(heading)
    return (position[0] + dr, position[1] + dc)


class Field:
    """A crop field with a farmyard, mutated in place as crop is harvested."""

    def __init__(self, grid: Grid, farm: Cell):
        self.grid = grid
        self.rows = len(grid)
        self.cols = len(grid[0])
        self.farm = farm
        if not self.inside(farm):
            raise ValueError(f"The farm at {farm} falls outside the field")
        if grid[farm[0]][farm[1]] == OBSTACLE:
            raise ValueError(f"The farm at {farm} sits on an obstacle")

    def inside(self, cell: Cell) -> bool:
        """True if `cell` is within the field boundaries."""
        row, col = cell
        return 0 <= row < self.rows and 0 <= col < self.cols

    def passable(self, cell: Cell) -> bool:
        """True if `cell` is on the field and free of obstacles."""
        return self.inside(cell) and self.grid[cell[0]][cell[1]] != OBSTACLE

    def drivable(self, cell: Cell, avoid_crop: bool = False) -> bool:
        """True if a machine can drive onto `cell`.

        `avoid_crop` is what separates the two classes of machine: a grain cart
        would flatten standing crop, so it may only use ground that has already
        been cut, while a harvester drives wherever there is no rock.
        """
        if not self.passable(cell):
            return False
        return not (avoid_crop and self.grid[cell[0]][cell[1]] == FOOD)

    def neighbors(self, cell: Cell, avoid_crop: bool = False) -> list[Cell]:
        """The drivable cells orthogonally adjacent to `cell`."""
        row, col = cell
        return [
            (row + dr, col + dc)
            for dr, dc in STEPS
            if self.drivable((row + dr, col + dc), avoid_crop)
        ]

    def has_food(self, cell: Cell) -> bool:
        """True if `cell` still holds crop."""
        return self.grid[cell[0]][cell[1]] == FOOD

    def harvest(self, cell: Cell) -> int:
        """Harvest `cell`, returning the grain units taken (1, or 0 if bare)."""
        if not self.has_food(cell):
            return 0
        self.grid[cell[0]][cell[1]] = EMPTY
        return 1

    def food_cells(self) -> set[Cell]:
        """Every cell that still holds crop."""
        return {
            (r, c)
            for r, row in enumerate(self.grid)
            for c, value in enumerate(row)
            if value == FOOD
        }

    def food_left(self) -> int:
        """How many crop cells are left in the whole field."""
        return sum(row.count(FOOD) for row in self.grid)
