"""Human-readable terminal output for recommendation results."""

from __future__ import annotations

from .models import FleetEvaluation, FleetRecommendation


def _fleet(evaluation: FleetEvaluation) -> str:
    fleet = evaluation.fleet
    harvesters = "cosechadora" if fleet.harvesters == 1 else "cosechadoras"
    carts = "grain cart" if fleet.carts == 1 else "grain carts"
    return f"{fleet.harvesters} {harvesters} / {fleet.carts} {carts}"


def _short_fleet(evaluation: FleetEvaluation) -> str:
    return f"{evaluation.fleet.harvesters}H/{evaluation.fleet.carts}C"


def format_evaluations_table(result: FleetRecommendation) -> str:
    """Render every eligible fleet, ordered by score and then vehicle count."""
    headers = (
        "Flotilla", "Dur.", "Espera", "Tasa esp.", "Espera máx.", "Fuel",
        "Fuel/u", "CO2", "Tráfico", "Tráf./u", "Índice", "Score", "Fin.",
        "Pareto", "Clasificación",
    )
    rows = []
    eligible = sorted(
        (item for item in result.evaluated_configurations if item.eligible),
        key=lambda item: (item.score, item.fleet.vehicles),
    )
    for item in eligible:
        labels = []
        if item is result.recommended:
            labels.append("balanceada")
        if item is result.sustainable_recommendation:
            labels.append("sostenible")
        if item is result.economical_alternative:
            labels.append("menor maquinaria")
        if item is result.maximum_productivity:
            labels.append("máx. productividad")
        if item in result.technical_tie:
            labels.append("empate técnico")
        rows.append(
            (
                _short_fleet(item),
                f"{item.means['duration_ticks']:.1f}",
                f"{item.means['cumulative_harvester_wait']:.1f}",
                f"{100 * item.means['harvester_wait_rate']:.2f}%",
                f"{item.means['max_harvester_wait']:.1f}",
                f"{item.means['fuel']:.1f}",
                f"{item.means['fuel_per_delivered_unit']:.3f}",
                f"{item.means['co2']:.1f}",
                f"{item.means['repeated_traffic']:.1f}",
                f"{item.means['traffic_per_delivered_unit']:.3f}",
                f"{item.fleet.machinery_index:.1f}",
                f"{item.score:.3f}",
                f"{100 * item.completion_rate:.0f}%",
                "sí" if item.is_pareto else "no",
                ", ".join(labels) or "—",
            )
        )
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def render(row) -> str:
        return " | ".join(value.ljust(width) for value, width in zip(row, widths))

    return "\n".join((render(headers), "-+-".join("-" * width for width in widths), *(render(row) for row in rows)))


def format_recommendation(result: FleetRecommendation) -> str:
    terrain = result.terrain
    chosen = result.recommended
    total_runs = sum(len(item.runs) for item in result.evaluated_configurations)
    completed = sum(
        run.completed
        for item in result.evaluated_configurations
        for run in item.runs
    )
    lines = [
        f"RECOMENDACIÓN PARA TERRENO {terrain.rows} × {terrain.cols}",
        f"Área: {terrain.area} celdas | Proporción: {terrain.aspect_ratio:.2f}",
        (
            f"Evaluación: {len(result.evaluated_configurations)} flotillas × "
            f"{len(result.seeds)} semillas = {total_runs} campañas"
        ),
        "",
        f"Recomendación balanceada: {_fleet(chosen)}",
        f"- Duración media: {chosen.means['duration_ticks']:.1f} ticks",
        f"- Espera acumulada media: {chosen.means['cumulative_harvester_wait']:.1f} ticks",
        f"- Tasa de espera: {100 * chosen.means['harvester_wait_rate']:.2f}%",
        f"- Espera máxima media: {chosen.means['max_harvester_wait']:.1f} ticks",
        f"- Combustible / CO₂: {chosen.means['fuel']:.1f} L / {chosen.means['co2']:.1f} kg",
        f"- Tráfico repetido: {chosen.means['repeated_traffic']:.1f} entradas",
        "",
        "Motivos:",
        *[f"- {reason}" for reason in result.reasons],
        "",
        (
            f"Recomendación sostenible: {_fleet(result.sustainable_recommendation)}"
            if result.sustainable_recommendation is not None
            else "Recomendación sostenible: no disponible"
        ),
        f"Alternativa con menor maquinaria: {_fleet(result.economical_alternative)}",
        f"Máxima productividad: {_fleet(result.maximum_productivity)}",
        (
            "Empate técnico: "
            + ", ".join(_short_fleet(item) for item in result.technical_tie)
            if len(result.technical_tie) > 1
            else "Empate técnico: no"
        ),
        f"Campañas completas: {completed}/{total_runs}",
        "",
        f"Confianza de la recomendación: {result.confidence}",
        *[f"- {warning}" for warning in result.stability_warnings],
        "",
        "CONFIGURACIONES ELEGIBLES",
        format_evaluations_table(result),
    ]
    rejected = [e for e in result.evaluated_configurations if not e.eligible]
    if rejected:
        lines.append(f"Flotillas descartadas: {len(rejected)}")
    return "\n".join(lines)
