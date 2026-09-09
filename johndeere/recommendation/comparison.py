"""Seed-paired comparisons between evaluated fleet configurations."""

from __future__ import annotations

import statistics

from .models import FleetEvaluation, PairedComparison


def compare_paired(
    origin: FleetEvaluation, destination: FleetEvaluation
) -> PairedComparison:
    """Compare destination against origin on their common terrain seeds."""
    origin_by_seed = {run.seed: run for run in origin.runs if not run.error}
    destination_by_seed = {run.seed: run for run in destination.runs if not run.error}
    common = sorted(origin_by_seed.keys() & destination_by_seed.keys())
    if not common:
        raise ValueError("Fleet evaluations have no common valid seeds")
    tick_differences = []
    percent_changes = []
    wait_differences = []
    fuel_differences = []
    traffic_differences = []
    wins = losses = ties = 0
    for seed in common:
        before = origin_by_seed[seed]
        after = destination_by_seed[seed]
        difference = after.duration_ticks - before.duration_ticks
        tick_differences.append(difference)
        percent_changes.append(
            0.0
            if before.duration_ticks == 0
            else 100.0 * difference / before.duration_ticks
        )
        wait_differences.append(
            after.cumulative_harvester_wait - before.cumulative_harvester_wait
        )
        fuel_differences.append(after.fuel - before.fuel)
        traffic_differences.append(after.repeated_traffic - before.repeated_traffic)
        wins += difference < 0
        losses += difference > 0
        ties += difference == 0
    return PairedComparison(
        origin=origin.fleet,
        destination=destination.fleet,
        seeds=len(common),
        wins=wins,
        losses=losses,
        ties=ties,
        mean_tick_difference=statistics.fmean(tick_differences),
        median_tick_difference=statistics.median(tick_differences),
        mean_percent_change=statistics.fmean(percent_changes),
        mean_wait_difference=statistics.fmean(wait_differences),
        mean_fuel_difference=statistics.fmean(fuel_differences),
        mean_traffic_difference=statistics.fmean(traffic_differences),
    )
