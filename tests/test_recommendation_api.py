"""API adaptation, budget, isolation, and observational-metric tests."""

from __future__ import annotations

import os
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from johndeere.config import SimulationConfig
from johndeere.recommendation.evaluator import recommendation_profiles, run_campaign
from johndeere.recommendation.models import (
    FleetConfig,
    FleetCostConfig,
    FleetEvaluation,
    FleetRecommendation,
    RunMetrics,
    TerrainSpec,
)
from johndeere.simulation import Simulation


def evaluated(h: int, c: int, duration: float, fuel: float) -> FleetEvaluation:
    item = FleetEvaluation(
        fleet=FleetConfig(h, c),
        runs=[RunMetrics(1, True, int(duration), fuel, fuel * 2.68)],
        means={
            "duration_ticks": duration,
            "fuel": fuel,
            "co2": fuel * 2.68,
            "repeated_traffic": 10.0,
            "harvester_wait_rate": 0.1,
        },
        eligible=True,
        score=duration / 1000,
    )
    return item


class CostAndProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.costs = FleetCostConfig("MXN", "test-v1", 100, 40)
        self.items = [evaluated(1, 1, 120, 20), evaluated(2, 1, 80, 30)]
        self.recommendation = FleetRecommendation(
            TerrainSpec.from_dimensions(10, 12),
            (1,),
            self.items[0],
            self.items[0],
            self.items[1],
            self.items,
            [],
        )

    def test_missing_cost_configuration_is_explicit(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing fleet cost configuration"):
            FleetCostConfig.from_mapping({})

    def test_cost_is_calculated_in_python(self) -> None:
        self.assertEqual(self.costs.estimate(FleetConfig(3, 2)), 380)

    def test_profiles_are_filtered_by_budget(self) -> None:
        profiles = recommendation_profiles(self.recommendation, 150, self.costs)
        self.assertEqual({row["profile"] for row in profiles}, {
            "balanced", "lower_consumption", "minimum_machinery", "minimum_duration"
        })
        self.assertTrue(all(row["estimatedCost"] <= 150 for row in profiles))
        self.assertTrue(all(row["budgetRemaining"] >= 0 for row in profiles))

    def test_insufficient_budget_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "insufficient"):
            recommendation_profiles(self.recommendation, 139, self.costs)


class EngineAdaptationTests(unittest.TestCase):
    def test_simulation_constructor_has_no_adaptive_policy(self) -> None:
        with self.assertRaises(TypeError):
            Simulation(SimulationConfig(rows=6, cols=6), adaptive_policy=None)

    def test_real_terrain_parameters_reach_simulation(self) -> None:
        terrain = TerrainSpec.from_dimensions(
            8, 9, food_ratio=0.75, border=2, min_obstacles=1, max_obstacles=2
        )
        seen = {}

        class FakeSimulation:
            def __init__(self, config):
                seen.update(vars(config))

            def run(self, on_tick=None):
                metrics = SimpleNamespace(
                    fuel=1.0, co2=2.68, idle_ticks=0, max_harvester_wait_ticks=0,
                    repeated_traffic=0, harvested=1, delivered=1, in_transit=0,
                    stranded=0,
                )
                return SimpleNamespace(
                    metrics=metrics, initial_grid=[[1]], unreachable_food=0,
                    completed=True, ticks=1, collisions=0, obstacle_violations=0,
                )

        with patch("johndeere.recommendation.evaluator.Simulation", FakeSimulation):
            run_campaign(terrain, FleetConfig(1, 1), 7)
        self.assertEqual(
            (seen["rows"], seen["cols"], seen["food_ratio"], seen["border"],
             seen["min_obstacles"], seen["max_obstacles"]),
            (8, 9, 0.75, 2, 1, 2),
        )

    def test_observational_metrics_do_not_change_campaign(self) -> None:
        config = SimulationConfig(
            rows=16, cols=22, harvesters=3, carts=2, food_ratio=1.0,
            border=1, min_obstacles=3, max_obstacles=5, seed=424242,
            max_ticks=5000,
        )
        simulation = Simulation(config)
        result = simulation.run()
        self.assertEqual(
            (result.ticks, result.completed, result.metrics.harvested,
             result.metrics.delivered, result.metrics.distance),
            (265, True, 275, 275, 740),
        )
        self.assertAlmostEqual(result.metrics.fuel, 519.8499999999981)
        self.assertGreaterEqual(result.metrics.max_harvester_wait_ticks, 0)
        self.assertGreaterEqual(result.metrics.repeated_traffic, 0)


class ActiveLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_loop_continues_during_recommendation(self) -> None:
        from Servidor.web import RecommendationService

        ticks = 0
        running = True

        async def drive():
            nonlocal ticks
            while running:
                ticks += 1
                await asyncio.sleep(0)

        driver = asyncio.create_task(drive())
        service = RecommendationService(timeout=1.0)
        await service.run(lambda: (time.sleep(0.03), {"ok": True})[1])
        running = False
        await driver
        self.assertGreater(ticks, 1)


@unittest.skipUnless(os.environ.get("RUN_STARLETTE_TESTS") == "1", "Starlette test deps not loaded")
class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient
        cls.TestClient = TestClient

    def environment(self, **extra):
        values = {
            "FLEET_RECOMMENDATIONS_ENABLED": "true",
            "FLEET_COST_CURRENCY": "MXN",
            "FLEET_COST_VERSION": "test-v1",
            "FLEET_HARVESTER_COST": "100",
            "FLEET_CART_COST": "40",
        }
        values.update(extra)
        return patch.dict(os.environ, values, clear=False)

    def payload(self):
        return {
            "schemaVersion": 1,
            "requestId": "rec-001",
            "terrain": {"rows": 8, "columns": 9, "border": 1,
                        "minObstacles": 0, "maxObstacles": 1},
            "budget": {"amount": 1000, "currency": "MXN"},
        }

    def app(self):
        from Servidor.web import build_web_app
        return build_web_app(SimpleNamespace(sim=object()))

    def test_feature_flag_and_existing_routes(self) -> None:
        from Servidor.web import build_web_app
        with patch.dict(os.environ, {"FLEET_RECOMMENDATIONS_ENABLED": "false"}):
            app = build_web_app(SimpleNamespace(sim=object()))
        routes = {(route.path, tuple(route.methods)) for route in app.routes}
        self.assertTrue(any(path == "/api/state" and "GET" in methods for path, methods in routes))
        response = self.TestClient(app).post("/api/fleet-recommendations", json=self.payload())
        self.assertEqual(response.status_code, 503)

    def test_request_id_currency_and_active_simulation_are_preserved(self) -> None:
        active = object()
        session = SimpleNamespace(sim=active)
        fake = {"costVersion": "test-v1", "terrain": {}, "budget": {}, "profiles": []}
        with self.environment(), patch("Servidor.web.recommend_fleet_profiles", return_value=fake):
            client = self.TestClient(__import__("Servidor.web", fromlist=["build_web_app"]).build_web_app(session))
            response = client.post("/api/fleet-recommendations", json=self.payload())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["requestId"], "rec-001")
            self.assertIs(session.sim, active)
            wrong = self.payload()
            wrong["budget"]["currency"] = "USD"
            self.assertEqual(client.post("/api/fleet-recommendations", json=wrong).status_code, 400)

    def test_timeout_keeps_concurrency_slot(self) -> None:
        def slow(*args, **kwargs):
            time.sleep(0.15)
            return {"costVersion": "test-v1", "terrain": {}, "budget": {}, "profiles": []}

        with self.environment(FLEET_RECOMMENDATION_TIMEOUT_SECONDS="0.01"), patch(
            "Servidor.web.recommend_fleet_profiles", side_effect=slow
        ):
            with self.TestClient(self.app()) as client:
                first = client.post("/api/fleet-recommendations", json=self.payload())
                second = client.post("/api/fleet-recommendations", json=self.payload())
        self.assertEqual(first.status_code, 504)
        self.assertEqual(first.json()["requestId"], "rec-001")
        self.assertEqual(second.status_code, 429)


if __name__ == "__main__":
    unittest.main()
