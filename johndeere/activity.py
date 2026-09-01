"""What each part of the architecture did during one tick.

The engine already coordinates the fleet; this is what makes that coordination
*visible*. Every counter maps to one box or one arrow of the architecture
diagram, so a frontend can light up the parts that actually did something.

Nothing here influences a decision — these are counters, read after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The boxes of the architecture diagram, in reading order.
BOXES = ("data", "engine", "assignment", "agents", "logistics", "telemetry")

#: The arrows between them: (from, to, label).
ARROWS = (
    ("data", "engine", "votes"),
    ("engine", "assignment", "coalitions"),
    ("engine", "agents", ""),
    ("engine", "logistics", ""),
    ("agents", "telemetry", ""),
    ("logistics", "telemetry", ""),
    ("telemetry", "engine", "feedback"),
)


@dataclass(frozen=True)
class TickActivity:
    """One tick's worth of work, counted per part of the architecture."""

    # data -> engine: harvesters asking to be emptied
    requests_posted: int = 0
    open_requests: int = 0

    # engine: the auction and the traffic control
    bids_evaluated: int = 0
    traffic_claims: int = 0
    traffic_refusals: int = 0
    give_ways: int = 0

    # engine -> assignment: carts paired to harvesters
    assignments: int = 0
    coalitions: int = 0

    # the two families of agent
    harvest_moves: int = 0
    cuts: int = 0
    rotations: int = 0
    waiting: int = 0
    cart_moves: int = 0
    transfers: int = 0
    deliveries: int = 0

    @property
    def data_active(self) -> bool:
        """A harvester published a request, or one is still open."""
        return bool(self.requests_posted or self.open_requests)

    @property
    def engine_active(self) -> bool:
        """The auction ran, or the traffic control had to arbitrate."""
        return bool(
            self.bids_evaluated or self.traffic_refusals or self.give_ways
        )

    @property
    def assignment_active(self) -> bool:
        """A cart was paired to a harvester this tick."""
        return bool(self.assignments)

    @property
    def agents_active(self) -> bool:
        """A harvester drove, cut, turned on the spot or stood waiting."""
        return bool(self.harvest_moves or self.cuts or self.rotations or self.waiting)

    @property
    def logistics_active(self) -> bool:
        """A cart drove, took grain aboard or emptied at the farm."""
        return bool(self.cart_moves or self.transfers or self.deliveries)

    @property
    def telemetry_active(self) -> bool:
        """Something happened that the machines reported back."""
        return bool(self.cuts or self.transfers or self.deliveries or self.waiting)

    def hot(self, box: str) -> bool:
        """Whether `box` of the architecture diagram did something this tick."""
        return bool(getattr(self, f"{box}_active"))

    def flowing(self, source: str, target: str) -> bool:
        """Whether information travelled down the arrow `source -> target`."""
        if (source, target) == ("data", "engine"):
            return bool(self.requests_posted or self.bids_evaluated)
        if (source, target) == ("engine", "assignment"):
            return bool(self.assignments)
        if (source, target) == ("engine", "agents"):
            return bool(self.harvest_moves or self.rotations)
        if (source, target) == ("engine", "logistics"):
            return bool(self.cart_moves or self.assignments)
        if (source, target) == ("agents", "telemetry"):
            return bool(self.cuts or self.waiting)
        if (source, target) == ("logistics", "telemetry"):
            return bool(self.transfers or self.deliveries)
        if (source, target) == ("telemetry", "engine"):
            return bool(self.open_requests or self.transfers)
        return False
