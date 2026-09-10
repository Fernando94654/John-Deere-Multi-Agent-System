"""Tests for the from-scratch fleet advisor and its `/api/fleet-recommendations` route."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from johndeere.fleet_advisor import (
    FleetCosts,
    Terrain,
    estimate_run,
    recommend_fleet,
)


COSTS = FleetCosts(currency="MXN", cost_version="test-v1", harvester_cost=100, cart_cost=40)


class CostConfigTests(unittest.TestCase):
    def test_from_env_reports_every_missing_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing fleet cost configuration"):
            FleetCosts.from_env({})
        with self.assertRaisesRegex(ValueError, "FLEET_CART_COST"):
            FleetCosts.from_env({
                "FLEET_COST_CURRENCY": "MXN", "FLEET_COST_VERSION": "v1",
                "FLEET_HARVESTER_COST": "100",
            })

    def test_from_env_rejects_non_integer_and_negative_prices(self) -> None:
        base = {"FLEET_COST_CURRENCY": "MXN", "FLEET_COST_VERSION": "v1"}
        with self.assertRaisesRegex(ValueError, "must be integers"):
            FleetCosts.from_env({**base, "FLEET_HARVESTER_COST": "abc", "FLEET_CART_COST": "40"})
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            FleetCosts.from_env({**base, "FLEET_HARVESTER_COST": "-1", "FLEET_CART_COST": "40"})

    def test_price_is_linear(self) -> None:
        self.assertEqual(COSTS.price(3, 2), 380)


class TerrainTests(unittest.TestCase):
    def test_rejects_impossible_fields(self) -> None:
        for bad in (
            Terrain(2, 40),
            Terrain(40, 40, border=25),
            Terrain(40, 40, min_obstacles=5, max_obstacles=3),
            Terrain(40, 40, food_ratio=1.5),
        ):
            with self.assertRaises(ValueError):
                bad.validated()

    def test_crop_and_capacity_scale_with_the_field(self) -> None:
        small = Terrain(16, 22).validated()
        big = Terrain(50, 50).validated()
        self.assertLess(small.crop, big.crop)
        self.assertLessEqual(small.max_harvesters, big.max_harvesters)
        self.assertGreaterEqual(small.max_harvesters, 1)


class EstimateTests(unittest.TestCase):
    def test_more_carts_relieve_a_cart_bottleneck(self) -> None:
        terrain = Terrain(20, 30).validated()
        one = estimate_run(terrain, harvesters=4, carts=1)
        two = estimate_run(terrain, harvesters=4, carts=2)
        self.assertEqual(one["bottleneck"], "carts")
        self.assertLess(two["duration"], one["duration"])
        self.assertLess(two["harvesterWaitRate"], one["harvesterWaitRate"])

    def test_more_harvesters_finish_sooner_with_diminishing_returns(self) -> None:
        terrain = Terrain(22, 22).validated()
        d1 = estimate_run(terrain, 1, 2)["duration"]
        d2 = estimate_run(terrain, 2, 2)["duration"]
        d4 = estimate_run(terrain, 4, 2)["duration"]
        self.assertGreater(d1, d2)
        self.assertGreater(d2, d4)
        self.assertGreater(d1 - d2, d2 - d4)  # returns diminish

    def test_matches_recorded_runs_within_tolerance(self) -> None:
        # (rows, cols, h, c) -> (recorded mean duration, recorded mean fuel)
        recorded = {
            (16, 22, 3, 2): (313, 572),
            (22, 22, 3, 2): (432, 765),
            (20, 30, 4, 2): (560, 1085),
            (12, 14, 2, 2): (136, 216),
        }
        for (r, c, h, ct), (dur, fuel) in recorded.items():
            est = estimate_run(Terrain(r, c).validated(), h, ct)
            self.assertLess(abs(est["duration"] - dur) / dur, 0.25, (r, c, h, ct, est))
            self.assertLess(abs(est["fuel"] - fuel) / fuel, 0.25, (r, c, h, ct, est))

    def test_is_fast_on_a_large_field(self) -> None:
        import time
        start = time.perf_counter()
        estimate_run(Terrain(200, 200).validated(), 12, 4)
        self.assertLess(time.perf_counter() - start, 0.05)


class RecommendTests(unittest.TestCase):
    def test_returns_the_four_profiles_in_a_stable_envelope(self) -> None:
        result = recommend_fleet(16, 22, budget=1000, costs=COSTS)
        self.assertEqual(result["costVersion"], "test-v1")
        self.assertEqual(result["budget"], {"amount": 1000, "currency": "MXN"})
        self.assertEqual(result["terrain"]["rows"], 16)
        names = [p["profile"] for p in result["profiles"]]
        self.assertEqual(set(names), {
            "minimum_duration", "minimum_machinery", "lower_consumption", "balanced",
        })
        for p in result["profiles"]:
            self.assertLessEqual(p["estimatedCost"], 1000)
            self.assertGreaterEqual(p["budgetRemaining"], 0)
            self.assertEqual(p["estimatedCost"], COSTS.price(p["harvesters"], p["carts"]))
            self.assertIn("duration", p["metrics"])
            self.assertIn("fuel", p["metrics"])
            self.assertIn("co2", p["metrics"])

    def test_budget_only_admits_affordable_fleets(self) -> None:
        # 240 buys at most two harvesters + one cart, or one harvester + three carts.
        result = recommend_fleet(16, 22, budget=240, costs=COSTS)
        for p in result["profiles"]:
            self.assertLessEqual(COSTS.price(p["harvesters"], p["carts"]), 240)

    def test_budget_below_the_cheapest_fleet_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cheapest workable fleet"):
            recommend_fleet(16, 22, budget=139, costs=COSTS)  # one H + one C = 140

    def test_minimum_duration_is_never_slower_than_the_others(self) -> None:
        result = recommend_fleet(24, 24, budget=2000, costs=COSTS)
        by_name = {p["profile"]: p for p in result["profiles"]}
        fastest = by_name["minimum_duration"]["metrics"]["duration"]
        for p in result["profiles"]:
            self.assertGreaterEqual(p["metrics"]["duration"], fastest)

    def test_large_field_recommendation_is_instant(self) -> None:
        import time
        start = time.perf_counter()
        result = recommend_fleet(50, 50, budget=5000, costs=COSTS)
        self.assertLess(time.perf_counter() - start, 0.2)
        self.assertEqual(len(result["profiles"]), 4)


@unittest.skipUnless(os.environ.get("RUN_STARLETTE_TESTS") == "1", "starlette test deps not loaded")
class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient
        cls.TestClient = TestClient

    def app(self):
        from Servidor.web import build_web_app
        return build_web_app(SimpleNamespace(sim=None, args=SimpleNamespace()))

    def enabled_env(self, **extra):
        return patch.dict(os.environ, {
            "FLEET_RECOMMENDATIONS_ENABLED": "true",
            "FLEET_COST_CURRENCY": "MXN", "FLEET_COST_VERSION": "test-v1",
            "FLEET_HARVESTER_COST": "100", "FLEET_CART_COST": "40",
            **extra,
        }, clear=False)

    def payload(self, **over):
        base = {
            "schemaVersion": 1,
            "requestId": "rec-1",
            "terrain": {"rows": 16, "columns": 22, "border": 1,
                        "minObstacles": 3, "maxObstacles": 5},
            "budget": {"amount": 1000, "currency": "MXN"},
        }
        base.update(over)
        return base

    def test_route_exists_and_existing_routes_untouched(self) -> None:
        app = self.app()
        paths = {r.path for r in app.routes}
        self.assertIn("/api/fleet-recommendations", paths)
        self.assertIn("/api/state", paths)
        self.assertIn("/api/chat", paths)

    def test_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {"FLEET_RECOMMENDATIONS_ENABLED": "false"}, clear=False):
            r = self.TestClient(self.app()).post("/api/fleet-recommendations", json=self.payload())
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "disabled")
        self.assertEqual(r.json()["requestId"], "rec-1")

    def test_schema_and_request_id_are_required(self) -> None:
        with self.enabled_env():
            c = self.TestClient(self.app())
            self.assertEqual(c.post("/api/fleet-recommendations", json={"requestId": "x"}).status_code, 400)
            self.assertEqual(
                c.post("/api/fleet-recommendations", json={"schemaVersion": 1}).status_code, 400
            )

    def test_completed_response_carries_profiles_and_request_id(self) -> None:
        with self.enabled_env():
            r = self.TestClient(self.app()).post("/api/fleet-recommendations", json=self.payload())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["requestId"], "rec-1")
        self.assertEqual(body["costVersion"], "test-v1")
        self.assertEqual(len(body["profiles"]), 4)

    def test_currency_mismatch_and_missing_costs(self) -> None:
        with self.enabled_env():
            bad = self.payload()
            bad["budget"]["currency"] = "USD"
            self.assertEqual(
                self.TestClient(self.app()).post("/api/fleet-recommendations", json=bad).status_code,
                400,
            )
        with patch.dict(os.environ, {"FLEET_RECOMMENDATIONS_ENABLED": "true"}, clear=True):
            r = self.TestClient(self.app()).post("/api/fleet-recommendations", json=self.payload())
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "error")

    def test_low_budget_is_a_clean_400(self) -> None:
        with self.enabled_env():
            r = self.TestClient(self.app()).post(
                "/api/fleet-recommendations", json=self.payload(budget={"amount": 50, "currency": "MXN"})
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["status"], "error")


if __name__ == "__main__":
    unittest.main()
