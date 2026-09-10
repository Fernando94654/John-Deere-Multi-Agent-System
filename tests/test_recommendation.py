"""Tests for fleet candidate generation, evaluation and selection."""

from __future__ import annotations

import unittest

from johndeere.recommendation.candidates import (
    MAX_TOTAL_CONFIGURATIONS,
    generate_candidates,
    refinement_neighbors,
)
from johndeere.recommendation.comparison import compare_paired
from johndeere.recommendation.evaluator import aggregate, run_campaign, shared_seeds
from johndeere.recommendation.formatting import format_evaluations_table
from johndeere.recommendation.models import (
    FleetConfig,
    FleetEvaluation,
    FleetRecommendation,
    RunMetrics,
    TerrainSpec,
)
from johndeere.recommendation.scoring import (
    assess_stability,
    build_reasons,
    choose,
    mark_marginal_upgrades,
    normalize,
    percent_change,
)
from recommend_fleet import parse_args


def evaluation(h: int, c: int, duration: float, wait: float, fuel: float, traffic: float) -> FleetEvaluation:
    return FleetEvaluation(
        fleet=FleetConfig(h, c),
        runs=[RunMetrics(1, True, int(duration), fuel, fuel * 2.68)],
        means={
            "duration_ticks": duration,
            "cumulative_harvester_wait": wait,
            "harvester_wait_rate": wait / (h * duration),
            "max_harvester_wait": wait / 2,
            "fuel": fuel,
            "fuel_per_delivered_unit": fuel,
            "co2": fuel * 2.68,
            "repeated_traffic": traffic,
            "traffic_per_delivered_unit": traffic,
        },
        completion_rate=1.0,
        eligible=True,
    )


def measured_run(seed: int, ticks: int, wait: int = 10, fuel: float = 20.0, traffic: int = 5) -> RunMetrics:
    return RunMetrics(
        seed=seed,
        completed=True,
        duration_ticks=ticks,
        fuel=fuel,
        co2=fuel * 2.68,
        cumulative_harvester_wait=wait,
        harvester_wait_rate=wait / ticks,
        max_harvester_wait=wait,
        repeated_traffic=traffic,
        fuel_per_delivered_unit=fuel,
        traffic_per_delivered_unit=traffic,
        harvested=1,
        delivered=1,
        expected_reachable_crop=1,
    )


class CandidateTests(unittest.TestCase):
    def test_candidates_are_unique_bounded_and_physical(self) -> None:
        candidates = generate_candidates(TerrainSpec.from_dimensions(30, 30))
        self.assertLessEqual(len(candidates), 10)
        self.assertEqual(len(candidates), len(set(candidates)))
        self.assertIn(FleetConfig(1, 1), candidates)
        self.assertTrue(all(1 <= fleet.carts <= fleet.harvesters for fleet in candidates))

    def test_shape_changes_range_for_equal_area(self) -> None:
        square = generate_candidates(TerrainSpec.from_dimensions(20, 20))
        long = generate_candidates(TerrainSpec.from_dimensions(10, 40))
        self.assertGreater(max(f.harvesters for f in square), max(f.harvesters for f in long))

    def test_small_field_only_uses_one_machine_pair(self) -> None:
        self.assertEqual(
            generate_candidates(TerrainSpec.from_dimensions(6, 6)),
            [FleetConfig(1, 1)],
        )

    def test_refinement_includes_cart_rich_neighbor_without_duplicates(self) -> None:
        evaluated = {FleetConfig(2, 1), FleetConfig(2, 2)}
        neighbors = refinement_neighbors(FleetConfig(2, 2), evaluated)
        self.assertIn(FleetConfig(2, 3), neighbors)
        self.assertEqual(len(neighbors), len(set(neighbors)))
        self.assertFalse(evaluated.intersection(neighbors))

    def test_refinement_respects_total_limit(self) -> None:
        evaluated = {FleetConfig(index, 1) for index in range(1, MAX_TOTAL_CONFIGURATIONS)}
        neighbors = refinement_neighbors(FleetConfig(3, 2), evaluated)
        self.assertLessEqual(len(evaluated) + len(neighbors), MAX_TOTAL_CONFIGURATIONS)


class SeedAndRunTests(unittest.TestCase):
    def test_seed_schedule_is_reproducible(self) -> None:
        self.assertEqual(shared_seeds(9, 5), shared_seeds(9, 5))
        self.assertEqual(len(set(shared_seeds(9, 5))), 5)

    def test_seed_schedule_accepts_thirty_and_rejects_out_of_range(self) -> None:
        self.assertEqual(len(shared_seeds(42, 30)), 30)
        with self.assertRaises(ValueError):
            shared_seeds(42, 4)
        with self.assertRaises(ValueError):
            shared_seeds(42, 31)

    def test_cli_advanced_seed_arguments(self) -> None:
        args = parse_args(
            ["16", "22", "--budget", "2500000", "--seeds", "20", "--base-seed", "99"]
        )
        self.assertEqual(
            (args.rows, args.cols, args.budget, args.seeds, args.base_seed),
            (16, 22, 2500000, 20, 99),
        )

    def test_consecutive_runs_are_isolated_and_reproducible(self) -> None:
        terrain = TerrainSpec.from_dimensions(8, 10)
        fleet = FleetConfig(1, 1)
        first = run_campaign(terrain, fleet, 101)
        run_campaign(terrain, FleetConfig(2, 1), 101)
        third = run_campaign(terrain, fleet, 101)
        self.assertEqual(first, third)
        self.assertTrue(first.completed)
        self.assertEqual(first.harvested, first.delivered)
        self.assertAlmostEqual(
            first.harvester_wait_rate,
            first.cumulative_harvester_wait
            / (fleet.harvesters * first.duration_ticks),
        )
        self.assertAlmostEqual(first.fuel_per_delivered_unit, first.fuel / first.delivered)

    def test_incomplete_or_undelivered_run_is_rejected(self) -> None:
        run = RunMetrics(
            seed=1,
            completed=False,
            duration_ticks=10,
            fuel=2.0,
            co2=5.36,
            harvested=4,
            delivered=3,
            expected_reachable_crop=4,
        )
        result = aggregate(FleetConfig(1, 1), [run])
        self.assertFalse(result.eligible)
        self.assertTrue(result.rejection_reasons)


class ScoringTests(unittest.TestCase):
    def test_normalization_does_not_include_co2_twice(self) -> None:
        items = [evaluation(1, 1, 100, 20, 30, 10), evaluation(2, 1, 80, 10, 40, 12)]
        normalize(items)
        self.assertNotIn("co2", items[0].normalized)
        self.assertIn("machinery_index", items[0].normalized)

    def test_equal_metric_range_normalizes_to_zero(self) -> None:
        items = [evaluation(1, 1, 100, 20, 30, 10), evaluation(2, 1, 100, 20, 30, 10)]
        normalize(items)
        self.assertEqual(items[0].normalized["fuel_per_delivered_unit"], 0.0)

    def test_selection_returns_three_valid_roles(self) -> None:
        items = [
            evaluation(1, 1, 150, 70, 30, 8),
            evaluation(2, 1, 100, 30, 38, 10),
            evaluation(2, 2, 94, 20, 47, 15),
        ]
        recommended, economical, productive = choose(items)
        self.assertTrue(recommended.eligible)
        self.assertLessEqual(economical.means["duration_ticks"], recommended.means["duration_ticks"] * 1.50)
        self.assertEqual(productive.fleet, FleetConfig(2, 2))

    def test_technical_tie_uses_productivity_knee(self) -> None:
        light = evaluation(2, 3, 322, 80, 529, 455)
        knee = evaluation(3, 3, 244, 101, 563, 496)
        extra = evaluation(3, 4, 238, 88, 572, 496)
        for item, score in zip((light, knee, extra), (0.301, 0.305, 0.315)):
            item.score = score
            item.is_pareto = True
        # choose() recalculates scores, so exercise the knee values through the
        # production threshold using normalized inputs with the same ordering.
        selected, _, _ = choose([light, knee, extra])
        self.assertNotEqual(selected.fleet, FleetConfig(3, 4))

    def test_small_gain_with_material_cost_marks_marginal_upgrade(self) -> None:
        previous = evaluation(2, 1, 100, 100, 100, 100)
        upgrade = evaluation(2, 2, 97, 97, 109, 111)
        mark_marginal_upgrades([previous, upgrade])
        self.assertTrue(upgrade.beyond_marginal_point)

    def test_percent_change(self) -> None:
        self.assertAlmostEqual(percent_change(100, 92), -8.0)


class ComparisonAndPresentationTests(unittest.TestCase):
    def test_paired_comparison_counts_wins_losses_and_ties(self) -> None:
        origin = aggregate(
            FleetConfig(2, 1),
            [measured_run(1, 100), measured_run(2, 100), measured_run(3, 100)],
        )
        destination = aggregate(
            FleetConfig(2, 2),
            [measured_run(1, 90), measured_run(2, 110), measured_run(3, 100)],
        )
        paired = compare_paired(origin, destination)
        self.assertEqual((paired.wins, paired.losses, paired.ties), (1, 1, 1))
        self.assertEqual(paired.median_tick_difference, 0)

    def test_reason_names_origin_destination_and_machine_types(self) -> None:
        origin = aggregate(FleetConfig(3, 2), [measured_run(1, 100)])
        recommended = aggregate(FleetConfig(3, 3), [measured_run(1, 90)])
        productive = aggregate(FleetConfig(4, 4), [measured_run(1, 85)])
        choose([origin, recommended, productive])
        reasons = build_reasons(
            recommended,
            origin,
            origin,
            productive,
            [recommended],
            [origin, recommended, productive],
        )
        joined = " ".join(reasons)
        self.assertIn("Pasar de 3H/2C a 3H/3C", joined)
        self.assertIn("añade 1 grain cart", joined)
        self.assertIn("Pasar de 3H/3C a 4H/4C", joined)
        self.assertIn("añade 1 cosechadora y añade 1 grain cart", joined)
        self.assertIn("En duración gana", joined)

    def test_table_is_sorted_by_score_then_vehicle_count(self) -> None:
        first = evaluation(1, 1, 120, 30, 20, 5)
        second = evaluation(2, 1, 90, 10, 30, 8)
        choose([first, second])
        result = FleetRecommendation(
            TerrainSpec.from_dimensions(10, 10),
            (1,),
            second,
            first,
            second,
            [first, second],
            [],
        )
        table = format_evaluations_table(result)
        ordered = sorted([first, second], key=lambda item: (item.score, item.fleet.vehicles))
        positions = [table.index(f"{item.fleet.harvesters}H/{item.fleet.carts}C") for item in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Pareto", table)
        self.assertIn("Índice", table)

    def test_close_scores_emit_warning(self) -> None:
        first = aggregate(FleetConfig(1, 1), [measured_run(1, 100), measured_run(2, 100)])
        second = aggregate(FleetConfig(2, 1), [measured_run(1, 99), measured_run(2, 99)])
        first.score, second.score = 0.10, 0.11
        confidence, warnings = assess_stability([first, second], first)
        self.assertEqual(confidence, "baja")
        self.assertTrue(any("scores" in warning for warning in warnings))

    def test_leave_one_out_detects_changed_recommendation(self) -> None:
        baseline = aggregate(
            FleetConfig(1, 1),
            [measured_run(1, 100, wait=0), measured_run(2, 100, wait=0), measured_run(3, 100, wait=0)],
        )
        variable = aggregate(
            FleetConfig(2, 1),
            [measured_run(1, 50, wait=0), measured_run(2, 50, wait=0), measured_run(3, 170, wait=0)],
        )
        recommended, _, _ = choose([baseline, variable])
        _, warnings = assess_stability([baseline, variable], recommended)
        self.assertTrue(any("eliminar una sola semilla" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
