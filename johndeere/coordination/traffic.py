"""Cell reservations: the traffic light that keeps machines from colliding.

Every agent that wants to move claims the cell it is about to enter. A claim is
granted only if nobody stands there and nobody else has claimed it this tick, so
two machines can never end up on the same cell.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ..world.grid import Cell


class TrafficControl:
    """One tick's worth of cell claims.

    The farmyard is exempt: it is a depot where machines park, spawn and queue
    to unload, so several of them may share that one cell.
    """

    def __init__(self, depot: Optional[Cell] = None) -> None:
        self.depot = depot
        self.occupied: dict[Cell, str] = {}
        self.claimed: dict[Cell, str] = {}
        self.refusals = 0

    def begin_tick(self, agents: Iterable) -> None:
        """Record where everyone stands before anybody moves."""
        self.occupied = {
            agent.position: agent.label
            for agent in agents
            if agent.position != self.depot
        }
        self.claimed = {}

    def claim(self, agent: str, cell: Cell, standing_on: Cell) -> bool:
        """Try to reserve `cell`; returns whether the agent may drive into it.

        `agent` is the machine's label, which is unique across the whole fleet —
        harvester and cart ids are numbered separately and would otherwise be
        mistaken for one another.

        A cell being vacated in this same tick still counts as taken: allowing
        the swap would let two machines drive through each other.
        """
        if cell == self.depot:
            self.occupied.pop(standing_on, None)
            return True

        holder = self.occupied.get(cell)
        if holder is not None and holder != agent:
            self.refusals += 1
            return False
        if self.claimed.get(cell, agent) != agent:
            self.refusals += 1
            return False
        self.claimed[cell] = agent
        self.occupied.pop(standing_on, None)
        self.occupied[cell] = agent
        return True

    def blocker_at(self, cell: Optional[Cell]) -> Optional[str]:
        """Which agent, if any, is holding `cell`."""
        if cell is None:
            return None
        return self.occupied.get(cell) or self.claimed.get(cell)
