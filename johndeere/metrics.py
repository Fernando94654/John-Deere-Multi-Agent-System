"""Counters that turn a run into numbers: yield, distance, fuel, CO2, waiting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .config import CO2_KG_PER_LITRE


@dataclass
class FleetMetrics:
    """Totals over the whole fleet at one point in time."""

    ticks: int = 0
    harvested: int = 0
    delivered: int = 0
    in_transit: int = 0
    distance: int = 0
    fuel: float = 0.0
    idle_ticks: int = 0
    traffic_refusals: int = 0

    @property
    def co2(self) -> float:
        """Kilograms of CO2 emitted by the fleet."""
        return self.fuel * CO2_KG_PER_LITRE

    @property
    def fuel_per_unit(self) -> float:
        """Litres burned per grain unit delivered — the efficiency headline."""
        return self.fuel / self.delivered if self.delivered else 0.0


def collect(agents: Iterable, **totals) -> FleetMetrics:
    """Roll the per-agent counters up into a `FleetMetrics`."""
    agents = list(agents)
    return FleetMetrics(
        distance=sum(agent.distance for agent in agents),
        fuel=sum(agent.fuel for agent in agents),
        idle_ticks=sum(agent.idle_ticks for agent in agents),
        **totals,
    )
