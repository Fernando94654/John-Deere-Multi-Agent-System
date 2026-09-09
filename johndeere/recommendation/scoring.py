"""Normalize campaign outcomes and select balanced fleet alternatives."""

from __future__ import annotations

import statistics

from .comparison import compare_paired
from .models import FleetEvaluation

SCORE_WEIGHTS = {
    "duration_ticks": 0.35,
    "harvester_wait_rate": 0.25,
    "fuel_per_delivered_unit": 0.15,
    "traffic_per_delivered_unit": 0.10,
    "machinery_index": 0.15,
}
TECHNICAL_TIE_THRESHOLD = 0.02
SUSTAINABLE_DURATION_FACTOR = 1.40
MIN_TIME_IMPROVEMENT_PCT = 5.0
MIN_WAIT_IMPROVEMENT_PCT = 5.0
SIGNIFICANT_FUEL_INCREASE_PCT = 8.0
SIGNIFICANT_TRAFFIC_INCREASE_PCT = 10.0
ECONOMICAL_DURATION_FACTOR = 1.50
PRODUCTIVITY_TOLERANCE = 0.01


def percent_change(old: float, new: float) -> float:
    """Signed percentage change, with a stable definition around zero."""
    if abs(old) < 1e-12:
        return 0.0 if abs(new) < 1e-12 else float("inf")
    return 100.0 * (new - old) / abs(old)


def normalize(evaluations: list[FleetEvaluation]) -> None:
    """Attach comparable 0..1 costs to every eligible configuration."""
    eligible = [evaluation for evaluation in evaluations if evaluation.eligible]
    if not eligible:
        return
    values = {
        name: [
            float(
                e.fleet.machinery_index
                if name == "machinery_index"
                else e.means[name]
            )
            for e in eligible
        ]
        for name in SCORE_WEIGHTS
    }
    bounds = {name: (min(items), max(items)) for name, items in values.items()}
    for evaluation in eligible:
        normalized = {}
        for name, (low, high) in bounds.items():
            value = float(
                evaluation.fleet.machinery_index
                if name == "machinery_index"
                else evaluation.means[name]
            )
            normalized[name] = 0.0 if high == low else (value - low) / (high - low)
        evaluation.normalized = normalized
        evaluation.score = sum(
            SCORE_WEIGHTS[name] * normalized[name] for name in SCORE_WEIGHTS
        )


def _dominates(left: FleetEvaluation, right: FleetEvaluation) -> bool:
    names = (
        "duration_ticks",
        "cumulative_harvester_wait",
        "fuel",
        "repeated_traffic",
    )
    left_values = [left.means[name] for name in names] + [left.fleet.machinery_index]
    right_values = [right.means[name] for name in names] + [right.fleet.machinery_index]
    return all(a <= b for a, b in zip(left_values, right_values)) and any(
        a < b for a, b in zip(left_values, right_values)
    )


def pareto_front(evaluations: list[FleetEvaluation]) -> list[FleetEvaluation]:
    eligible = [evaluation for evaluation in evaluations if evaluation.eligible]
    return [
        candidate
        for candidate in eligible
        if not any(
            other is not candidate and _dominates(other, candidate)
            for other in eligible
        )
    ]


def mark_marginal_upgrades(evaluations: list[FleetEvaluation]) -> None:
    """Mark fleets whose every direct machinery upgrade has saturated."""
    by_fleet = {e.fleet: e for e in evaluations if e.eligible}
    for current in by_fleet.values():
        predecessors = [
            previous
            for fleet, previous in by_fleet.items()
            if (
                fleet.harvesters == current.fleet.harvesters - 1
                and fleet.carts == current.fleet.carts
            )
            or (
                fleet.harvesters == current.fleet.harvesters
                and fleet.carts == current.fleet.carts - 1
            )
        ]
        if not predecessors:
            continue
        unjustified = []
        for previous in predecessors:
            time_improvement = -percent_change(
                previous.means["duration_ticks"], current.means["duration_ticks"]
            )
            wait_improvement = -percent_change(
                previous.means["cumulative_harvester_wait"],
                current.means["cumulative_harvester_wait"],
            )
            fuel_increase = percent_change(previous.means["fuel"], current.means["fuel"])
            traffic_increase = percent_change(
                previous.means["repeated_traffic"], current.means["repeated_traffic"]
            )
            small_gain = (
                time_improvement < MIN_TIME_IMPROVEMENT_PCT
                and wait_improvement < MIN_WAIT_IMPROVEMENT_PCT
            )
            material_cost = (
                fuel_increase >= SIGNIFICANT_FUEL_INCREASE_PCT
                or traffic_increase >= SIGNIFICANT_TRAFFIC_INCREASE_PCT
            )
            unjustified.append(small_gain and material_cost)
        current.beyond_marginal_point = all(unjustified)


def choose(
    evaluations: list[FleetEvaluation],
) -> tuple[FleetEvaluation, FleetEvaluation, FleetEvaluation]:
    """Return the balanced, economical and maximum-productivity choices."""
    normalize(evaluations)
    mark_marginal_upgrades(evaluations)
    eligible = [evaluation for evaluation in evaluations if evaluation.eligible]
    if not eligible:
        raise ValueError("No fleet completed and delivered every campaign")
    front = pareto_front(evaluations)
    for evaluation in evaluations:
        evaluation.is_pareto = evaluation in front
    balanced_pool = [e for e in front if not e.beyond_marginal_point]
    if not balanced_pool:
        balanced_pool = eligible
    best_score = min(e.score for e in balanced_pool)
    tied = [e for e in balanced_pool if e.score - best_score < TECHNICAL_TIE_THRESHOLD]
    # Walk the technical tie from lighter to heavier machinery and stop at its
    # productivity knee instead of declaring either extreme the unique winner.
    ordered_tie = sorted(
        tied, key=lambda e: (e.fleet.machinery_index, e.means["duration_ticks"])
    )
    recommended = ordered_tie[0]
    for candidate in ordered_tie[1:]:
        improvement = -percent_change(
            recommended.means["duration_ticks"], candidate.means["duration_ticks"]
        )
        if improvement < MIN_TIME_IMPROVEMENT_PCT:
            break
        recommended = candidate
    affordable = [
        e
        for e in eligible
        if e.means["duration_ticks"]
        <= recommended.means["duration_ticks"] * ECONOMICAL_DURATION_FACTOR
    ]
    economical = min(
        affordable,
        key=lambda e: (
            e.fleet.machinery_index,
            e.fleet.vehicles,
            e.fleet.harvesters,
            e.score,
        ),
    )
    fastest_time = min(e.means["duration_ticks"] for e in eligible)
    fastest = [
        e
        for e in eligible
        if e.means["duration_ticks"] <= fastest_time * (1 + PRODUCTIVITY_TOLERANCE)
    ]
    productive = min(fastest, key=lambda e: (e.fleet.vehicles, e.score))
    return recommended, economical, productive


def technical_finalists(evaluations: list[FleetEvaluation]) -> list[FleetEvaluation]:
    """Return every eligible fleet within the technical score-tie band."""
    eligible = [e for e in evaluations if e.eligible and e.score is not None]
    if not eligible:
        return []
    best = min(e.score for e in eligible)
    return sorted(
        (e for e in eligible if e.score - best < TECHNICAL_TIE_THRESHOLD),
        key=lambda e: (e.score, e.fleet.vehicles),
    )


def choose_sustainable(
    evaluations: list[FleetEvaluation], balanced: FleetEvaluation
) -> FleetEvaluation:
    """Choose the lightest environmental profile among productive Pareto fleets."""
    pool = [
        e
        for e in evaluations
        if e.eligible
        and e.is_pareto
        and e.means["duration_ticks"]
        <= balanced.means["duration_ticks"] * SUSTAINABLE_DURATION_FACTOR
    ]
    if not pool:
        return balanced

    def normalized(name: str, evaluation: FleetEvaluation) -> float:
        values = [
            e.fleet.machinery_index if name == "machinery_index" else e.means[name]
            for e in pool
        ]
        low, high = min(values), max(values)
        value = (
            evaluation.fleet.machinery_index
            if name == "machinery_index"
            else evaluation.means[name]
        )
        return 0.0 if low == high else (value - low) / (high - low)

    return min(
        pool,
        key=lambda e: (
            0.45 * normalized("fuel_per_delivered_unit", e)
            + 0.35 * normalized("traffic_per_delivered_unit", e)
            + 0.20 * normalized("machinery_index", e),
            e.means["duration_ticks"],
        ),
    )


def _label(evaluation: FleetEvaluation) -> str:
    return f"{evaluation.fleet.harvesters}H/{evaluation.fleet.carts}C"


def _machinery_change(origin: FleetEvaluation, destination: FleetEvaluation) -> str:
    changes = []
    for delta, singular, plural in (
        (destination.fleet.harvesters - origin.fleet.harvesters, "cosechadora", "cosechadoras"),
        (destination.fleet.carts - origin.fleet.carts, "grain cart", "grain carts"),
    ):
        if delta:
            action = "añade" if delta > 0 else "retira"
            count = abs(delta)
            changes.append(f"{action} {count} {singular if count == 1 else plural}")
    return " y ".join(changes) if changes else "no cambia la maquinaria"


def _comparison_reason(origin: FleetEvaluation, destination: FleetEvaluation) -> str:
    paired = compare_paired(origin, destination)
    duration = paired.mean_percent_change
    wait = percent_change(
        origin.means["harvester_wait_rate"],
        destination.means["harvester_wait_rate"],
    )
    fuel = percent_change(origin.means["fuel"], destination.means["fuel"])
    traffic = percent_change(
        origin.means["repeated_traffic"], destination.means["repeated_traffic"]
    )
    if paired.wins > paired.losses:
        conclusion = "la mejora de duración es consistente por semilla"
    elif paired.wins < paired.losses:
        conclusion = "no se justifica por duración: pierde en la mayoría de semillas"
    else:
        conclusion = "el resultado marginal no es concluyente"
    return (
        f"Pasar de {_label(origin)} a {_label(destination)} {_machinery_change(origin, destination)}; "
        f"duración {duration:+.1f}%, espera {wait:+.1f}%, combustible {fuel:+.1f}% y "
        f"tráfico repetido {traffic:+.1f}%. En duración gana {paired.wins}/{paired.seeds} "
        f"semillas, pierde {paired.losses}/{paired.seeds} y empata "
        f"{paired.ties}/{paired.seeds}; "
        f"diferencia media {paired.mean_tick_difference:+.1f} ticks y mediana "
        f"{paired.median_tick_difference:+.1f}; "
        f"{conclusion}."
    )


def build_reasons(
    recommended: FleetEvaluation,
    sustainable: FleetEvaluation,
    economical: FleetEvaluation,
    productive: FleetEvaluation,
    finalists: list[FleetEvaluation],
    evaluations: list[FleetEvaluation],
) -> list[str]:
    """Explain the choice through exact, seed-paired fleet transitions."""
    reasons = [
        "Equilibra duración, espera de cosechadoras, combustible, tráfico repetido y tamaño de flotilla.",
        f"Completó el 100% de las {len(recommended.runs)} campañas evaluadas.",
    ]
    comparisons: list[tuple[FleetEvaluation, FleetEvaluation]] = []
    for finalist in finalists:
        if finalist is not recommended:
            origin, destination = sorted(
                (finalist, recommended),
                key=lambda e: e.means["duration_ticks"],
                reverse=True,
            )
            comparisons.append((origin, destination))
    if sustainable is not recommended:
        origin, destination = sorted(
            (sustainable, recommended),
            key=lambda e: e.means["duration_ticks"],
            reverse=True,
        )
        comparisons.append((origin, destination))
    if economical is not recommended:
        comparisons.append((economical, recommended))
    if productive is not recommended:
        comparisons.append((recommended, productive))
    for predecessor in evaluations:
        if not predecessor.eligible:
            continue
        direct = (
            predecessor.fleet.harvesters == recommended.fleet.harvesters - 1
            and predecessor.fleet.carts == recommended.fleet.carts
        ) or (
            predecessor.fleet.harvesters == recommended.fleet.harvesters
            and predecessor.fleet.carts == recommended.fleet.carts - 1
        )
        if direct:
            comparisons.append((predecessor, recommended))
    seen = set()
    for origin, destination in comparisons:
        key = (origin.fleet, destination.fleet)
        if key not in seen:
            reasons.append(_comparison_reason(origin, destination))
            seen.add(key)
    return reasons


def assess_stability(
    evaluations: list[FleetEvaluation], recommended: FleetEvaluation
) -> tuple[str, list[str]]:
    """Run inexpensive score-gap, paired, variability and leave-one-out checks."""
    eligible = sorted(
        (e for e in evaluations if e.eligible),
        key=lambda e: (e.score, e.fleet.vehicles),
    )
    warnings = []
    low_confidence = False
    if len(eligible) > 1:
        second = next(e for e in eligible if e is not recommended)
        if abs(second.score - recommended.score) < TECHNICAL_TIE_THRESHOLD:
            warnings.append("Las dos mejores configuraciones tienen scores muy próximos.")
            low_confidence = True
        paired = compare_paired(second, recommended)
        if paired.win_rate < 0.60:
            warnings.append(
                "La recomendación gana en menos del 60% de semillas frente a la segunda."
            )
    for name, label in (
        ("duration_ticks", "duración"),
        ("harvester_wait_rate", "tasa de espera"),
        ("fuel_per_delivered_unit", "combustible por unidad entregada"),
        ("traffic_per_delivered_unit", "tráfico por unidad entregada"),
    ):
        mean = recommended.means[name]
        deviation = recommended.standard_deviations[name]
        if mean and deviation / abs(mean) > 0.20:
            warnings.append(f"La métrica de {label} presenta variabilidad alta.")

    seeds = [run.seed for run in recommended.runs]
    changed = False
    for omitted in seeds:
        reduced = []
        for evaluation in eligible:
            runs = [run for run in evaluation.runs if run.seed != omitted]
            means = {
                name: statistics.fmean(getattr(run, name) for run in runs)
                for name in evaluation.means
            }
            deviations = {
                name: statistics.pstdev(getattr(run, name) for run in runs)
                for name in evaluation.standard_deviations
            }
            reduced.append(
                FleetEvaluation(
                    fleet=evaluation.fleet,
                    runs=runs,
                    means=means,
                    standard_deviations=deviations,
                    completion_rate=1.0,
                    eligible=True,
                )
            )
        if choose(reduced)[0].fleet != recommended.fleet:
            changed = True
            break
    if changed:
        warnings.append("La recomendación cambia al eliminar una sola semilla.")
        low_confidence = True
    confidence = "baja" if low_confidence else "moderada" if warnings else "alta"
    return confidence, warnings
