"""Route planning over a field with impassable obstacles.

Obstacles cannot be driven over, so no route is a straight line any more: every
move between two cells goes through `a_star`, and every notion of "how far is
that" goes through `bfs_distances`.
"""

from __future__ import annotations

import heapq
from collections import deque
from typing import Iterable, Optional

from ..world.field import Field
from ..world.grid import Cell


def manhattan(a: Cell, b: Cell) -> int:
    """Grid distance ignoring obstacles — an admissible A* heuristic."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def a_star(
    field: Field,
    start: Cell,
    goal: Cell,
    blocked: Iterable[Cell] = (),
    avoid_crop: bool = False,
) -> Optional[list[Cell]]:
    """Shortest drivable route from `start` to `goal`, `start` excluded.

    Returns the cells to drive through in order, or `None` when the goal cannot
    be reached. `blocked` holds cells to treat as temporarily impassable, which
    is how an agent plans around another machine standing in its way, and
    `avoid_crop` restricts the search to ground that has already been cut.
    """
    if start == goal:
        return []
    avoid = set(blocked)
    if not field.drivable(goal, avoid_crop) or goal in avoid:
        return None

    came_from: dict[Cell, Cell] = {}
    best_cost: dict[Cell, int] = {start: 0}
    queue: list[tuple[int, int, Cell]] = [(manhattan(start, goal), 0, start)]

    while queue:
        _, cost, current = heapq.heappop(queue)
        if current == goal:
            path = [current]
            while path[-1] != start:
                path.append(came_from[path[-1]])
            path.reverse()
            return path[1:]
        if cost > best_cost.get(current, cost):
            continue
        for neighbor in field.neighbors(current, avoid_crop):
            if neighbor in avoid:
                continue
            new_cost = cost + 1
            if new_cost >= best_cost.get(neighbor, new_cost + 1):
                continue
            best_cost[neighbor] = new_cost
            came_from[neighbor] = current
            heapq.heappush(
                queue, (new_cost + manhattan(neighbor, goal), new_cost, neighbor)
            )
    return None


def bfs_distances(
    field: Field,
    source: Cell,
    avoid_crop: bool = False,
    blocked: Iterable[Cell] = (),
) -> dict[Cell, int]:
    """Driving distance from `source` to every cell it can reach."""
    avoid = set(blocked)
    distances = {source: 0}
    queue = deque([source])
    while queue:
        current = queue.popleft()
        for neighbor in field.neighbors(current, avoid_crop):
            if neighbor not in distances and neighbor not in avoid:
                distances[neighbor] = distances[current] + 1
                queue.append(neighbor)
    return distances


def reachable_cells(field: Field, source: Cell) -> set[Cell]:
    """Every cell a machine starting at `source` can drive to.

    Crop walled in by obstacles is unreachable and must be excluded from the
    work plan — otherwise the run could never finish.
    """
    return set(bfs_distances(field, source))
