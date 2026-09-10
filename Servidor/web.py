"""HTTP + SSE dashboard API over a running simulation.

`server.py` streams the world to Unity over WebSocket and, with `--with-mcp`,
exposes it as MCP tools. This module adds the third audience: a browser. It
serves a small dashboard and a JSON API shaped for charts — time series, KPIs,
per-machine state and the supervisor's audit trail — plus the same command
surface the MCP tools cover, so an operator can steer the run from the page.

Like the MCP app it is a Starlette ASGI app served by uvicorn as a task on the
bridge's own event loop, reading the one shared `Session`. Its handlers do no
awaiting between reading engine state and serialising it, so a request and a
tick never interleave and the engine's invariants hold without a lock.

Every route is documented in `Servidor/WEB_API.md`.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from johndeere.config import (
    BLOCKED_TICKS_BEFORE_REROUTE,
    CART_CAPACITY,
    CO2_KG_PER_LITRE,
    FUEL_PER_CELL_CART,
    FUEL_PER_CELL_HARVESTER,
    FUEL_PER_IDLE_TICK,
    HARVESTER_TANK,
    REQUEST_THRESHOLD,
    TRANSFER_RATE,
    UNLOAD_TICKS,
    WAIT_WEIGHT,
)
from johndeere.recommendation import FleetCostConfig, recommend_fleet_profiles
from johndeere.world.grid import OBSTACLE
from Servidor.chat import OpenClawChat
from Servidor.controls import RunControls

DASHBOARD = os.path.join(
    os.path.dirname(__file__), os.pardir, "frontends", "dashboard", "index.html"
)

#: How long the SSE stream will sit silent before sending a comment to keep the
#: connection (and any proxy in front of it) from timing the socket out.
SSE_KEEPALIVE = 15


class RecommendationBusy(RuntimeError):
    pass


class RecommendationService:
    """Run one independent CPU-bound recommendation at a time."""

    def __init__(self, timeout: float):
        self.timeout = timeout
        self._lock: Optional[asyncio.Lock] = None

    async def _release_when_done(self, task: asyncio.Task) -> None:
        try:
            await task
        except Exception:
            pass
        finally:
            if self._lock is not None and self._lock.locked():
                self._lock.release()

    async def run(self, function) -> dict:
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._lock.locked():
            raise RecommendationBusy("a fleet recommendation is already running")
        await self._lock.acquire()
        task = asyncio.create_task(asyncio.to_thread(function))
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.timeout)
        finally:
            if task.done():
                self._lock.release()
            else:
                # A timed-out worker thread cannot be cancelled. Retain the
                # concurrency slot until it actually finishes.
                asyncio.create_task(self._release_when_done(task))


# --------------------------------------------------------------------------
# Serialisation: engine objects into the JSON the dashboard reads
# --------------------------------------------------------------------------

def config_payload(session) -> dict:
    """Every parameter of the run: the tunable ones and the hardcoded ones.

    This is the same set that feeds Unity. The `run` block is what a form on the
    page would POST back to `/api/config`; the rest is read-only reference.
    """
    policy = session.sim.policy if session.sim is not None else None
    return {
        "run": {
            "rows": session.rows,
            "cols": session.cols,
            "harvesters": session.n_harvesters,
            "carts": session.n_carts,
            "border": session.args.border,
            "minObstacles": session.min_obstacles,
            "maxObstacles": session.max_obstacles,
            "seed": session.seed,
            "tickInterval": session.args.delay,
            "maxTicks": session.sim.config.max_ticks if session.sim else None,
        },
        "machineSpecs": {
            "harvesterTank": HARVESTER_TANK,
            "cartCapacity": CART_CAPACITY,
            "transferRate": TRANSFER_RATE,
            "requestThreshold": REQUEST_THRESHOLD,
            "unloadTicks": UNLOAD_TICKS,
        },
        "sustainability": {
            "fuelPerCellHarvester": FUEL_PER_CELL_HARVESTER,
            "fuelPerCellCart": FUEL_PER_CELL_CART,
            "fuelPerIdleTick": FUEL_PER_IDLE_TICK,
            "co2KgPerLitre": CO2_KG_PER_LITRE,
        },
        "coordination": {
            "blockedTicksBeforeReroute": BLOCKED_TICKS_BEFORE_REROUTE,
            "waitWeight": WAIT_WEIGHT,
        },
        # The live, retunable knobs — a copy of `set_policy`'s inputs.
        "policy": {
            "requestThreshold": policy.request_threshold if policy else REQUEST_THRESHOLD,
            "waitWeight": policy.wait_weight if policy else WAIT_WEIGHT,
        },
    }


def _machine_extra(agent) -> dict:
    """The cumulative counters `diagnostics()` leaves off, per machine."""
    return {
        "distance": agent.distance,
        "fuel": round(agent.fuel, 2),
        "co2": round(agent.co2, 2),
        "routeLen": len(agent.route),
        "heading": {"row": agent.heading[0], "column": agent.heading[1]},
    }


def state_payload(session) -> dict:
    """The current tick, judged rather than drawn: KPIs, per-machine rows, the
    supervisor's caption. This is `Simulation.diagnostics()` plus the fleet
    metrics and a few derived headline numbers."""
    sim = session.sim
    base = {
        "status": session.status,
        "runId": session.run_id,
        "ready": sim is not None,
    }
    if sim is None:
        base["tick"] = 0
        return base

    diag = sim.diagnostics()
    metrics = sim._metrics()
    initial = session.initial_crop or 1

    harvesters = [
        {
            **row,
            **_machine_extra(machine),
            "tankPct": round(row["load"] / row["capacity"], 3) if row["capacity"] else 0.0,
        }
        for row, machine in zip(diag["harvesters"], sim.harvesters)
    ]
    carts = [
        {
            **row,
            **_machine_extra(machine),
            "loadPct": round(row["load"] / row["capacity"], 3) if row["capacity"] else 0.0,
        }
        for row, machine in zip(diag["carts"], sim.carts)
    ]

    return {
        **base,
        "tick": diag["tick"],
        "finished": diag["finished"],
        "narration": session.narration or {"text": "", "tick": 0},
        "kpi": {
            # The headline: share of the fleet actually working, not waiting.
            "utilization": round(1 - diag["idle_ratio"], 4),
            "idleRatio": diag["idle_ratio"],
            "fieldComplete": round(1 - diag["crop_left"] / initial, 4),
            "cropLeft": diag["crop_left"],
            "cropUnreachable": diag["crop_unreachable"],
            "openRequests": len(diag["open_requests"]),
            "fuelPerUnit": round(metrics.fuel_per_unit, 4),
            "rebalances": diag["rebalances"],
        },
        "metrics": {
            "harvested": metrics.harvested,
            "delivered": metrics.delivered,
            "inTransit": metrics.in_transit,
            "stranded": metrics.stranded,
            "distance": metrics.distance,
            "fuel": round(metrics.fuel, 2),
            "co2": round(metrics.co2, 2),
            "idleTicks": metrics.idle_ticks,
            "trafficRefusals": metrics.traffic_refusals,
            "collisions": sim.collisions,
            "obstacleViolations": sim.obstacle_violations,
            "auctionsRun": sim.dispatcher.auctions_run,
        },
        "policy": {
            "requestThreshold": diag["policy"]["request_threshold"],
            "waitWeight": diag["policy"]["wait_weight"],
        },
        "harvesters": harvesters,
        "carts": carts,
        "openRequests": diag["open_requests"],
        # {machine label: {state name: ticks spent in it}} since the run began.
        "stateHistogram": session.state_ticks,
    }


def field_payload(session) -> dict:
    """The grid as flat arrays for a minimap: crop, zone ownership, rocks, the
    machines. Cheap to poll at a low rate; it is the heavy part of the Unity
    payload, and the charts do not need it every tick."""
    sim = session.sim
    if sim is None:
        return {"ready": False}
    return {
        "ready": True,
        "tick": sim.tick,
        "runId": session.run_id,
        "rows": sim.field.rows,
        "columns": sim.field.cols,
        "farm": {"row": sim.farm[0], "column": sim.farm[1]},
        # row-major, rows*columns long: -1 rock, 0 cut, 1 standing crop.
        "crop": [value for row in sim.field.grid for value in row],
        # owning harvester id per cell, -1 for unowned.
        "zones": sim.zone_map(),
        "obstacles": [
            {"row": r, "column": c}
            for r, row in enumerate(sim.initial_grid)
            for c, value in enumerate(row)
            if value == OBSTACLE
        ],
        "harvesters": [
            {
                "id": h.label,
                "row": h.position[0],
                "column": h.position[1],
                "state": h.state.value,
            }
            for h in sim.harvesters
        ],
        "carts": [
            {
                "id": c.label,
                "row": c.position[0],
                "column": c.position[1],
                "state": c.state.value,
            }
            for c in sim.carts
        ],
    }


def history_payload(session, since: int) -> dict:
    """The per-tick sample buffer, for a chart to backfill when the page opens
    mid-run. `since` is exclusive; pass the last tick you already hold."""
    rows = [sample for sample in session.history if sample["tick"] > since]
    return {
        "runId": session.run_id,
        "from": rows[0]["tick"] if rows else since,
        "to": rows[-1]["tick"] if rows else since,
        "count": len(rows),
        "samples": rows,
    }


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _json_body(request: Request) -> dict:
    try:
        raw = await request.body()
    except Exception:  # pragma: no cover - transport
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("invalid JSON body") from None
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data


def _clamp(value, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return low


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------

def build_web_app(session, token: Optional[str] = None) -> Starlette:
    """The dashboard app bound to one `Session` — the shared, canonical run."""

    recommendation_enabled = os.getenv(
        "FLEET_RECOMMENDATIONS_ENABLED", "false"
    ).lower() in ("1", "true", "yes", "on")
    try:
        recommendation_timeout = float(
            os.getenv("FLEET_RECOMMENDATION_TIMEOUT_SECONDS", "30")
        )
    except ValueError:
        recommendation_timeout = 30.0
    recommendation_service = RecommendationService(max(0.01, recommendation_timeout))

    def authorized(request: Request) -> bool:
        if not token:
            return True
        return request.headers.get("authorization", "") == f"Bearer {token}"

    def unauthorized() -> JSONResponse:
        return JSONResponse({"error": "bad or missing bearer token"}, status_code=401)

    def log(tool: str, arguments: dict, detail: str) -> None:
        """Put a web-issued change in the same audit trail the MCP tools use."""
        tick = session.sim.tick if session.sim is not None else 0
        session.guard.record(tick, f"web:{tool}", arguments, True, detail)

    # --- reads ---------------------------------------------------------------

    async def get_index(request: Request) -> Response:
        if os.path.exists(DASHBOARD):
            return FileResponse(DASHBOARD)
        return JSONResponse(
            {"error": "frontends/dashboard/index.html is missing"}, status_code=404
        )

    async def get_config(request: Request) -> Response:
        return JSONResponse(config_payload(session))

    async def get_state(request: Request) -> Response:
        return JSONResponse(state_payload(session))

    async def get_field(request: Request) -> Response:
        return JSONResponse(field_payload(session))

    async def get_history(request: Request) -> Response:
        since = _clamp(request.query_params.get("since", -1), -1, 10**9)
        return JSONResponse(history_payload(session, since))

    async def get_events(request: Request) -> Response:
        limit = _clamp(request.query_params.get("limit", 20), 1, 200)
        return JSONResponse({"events": session.watcher.recent(limit)})

    async def get_decisions(request: Request) -> Response:
        limit = _clamp(request.query_params.get("limit", 20), 1, 200)
        return JSONResponse({"decisions": session.guard.log(limit)})

    async def get_runs(request: Request) -> Response:
        live = []
        if session.sim is not None and session.sim.tick > 0:
            live = [{**session.run_summary(), "live": True}]
        return JSONResponse({"runs": session.runs + live})

    async def stream_state(request: Request) -> Response:
        async def body():
            # Prime the stream so a subscriber that joins between ticks is not
            # staring at a blank page until the next one.
            yield _sse(state_payload(session))
            while True:
                if await request.is_disconnected():
                    break
                try:
                    await asyncio.wait_for(
                        session.wait_for_state(), timeout=SSE_KEEPALIVE
                    )
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(state_payload(session))

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- writes -----------------------------------------------------------
    # Each maps onto a method that already exists, on the Session or the engine;
    # nothing here steers a machine.

    controls = RunControls(session)

    async def write(request, operation, name):
        if not authorized(request):
            return unauthorized()
        try:
            body = await _json_body(request)
            result = operation(body)
        except (ValueError, TypeError, KeyError) as error:
            return JSONResponse({"error": str(error)}, status_code=getattr(error, "status", 400))
        log(name, body, str(result))
        return JSONResponse(result)

    async def post_command(request):
        action = request.path_params['action']
        return await write(request, lambda body: controls.command(action, body), f'command:{action}')

    async def config_endpoint(request):
        if request.method == 'POST':
            return await write(request, lambda body: controls.command('config', body), 'config')
        return await get_config(request)

    async def post_policy(request):
        return await write(request, lambda b: controls.policy(
            b.get('requestThreshold', b.get('request_threshold')),
            b.get('waitWeight', b.get('wait_weight'))), 'set_policy')

    async def post_rebalance(request):
        return await write(request, lambda _: controls.rebalance(), 'rebalance')

    async def post_cart(request):
        def apply(_):
            result = controls.add_cart()
            return {**result, 'fleetCarts': result['fleet_carts']}
        return await write(request, apply, 'add_cart')

    async def post_prioritize(request):
        def apply(body):
            result = controls.prioritize(*(body.get(camel, body.get(snake)) for camel, snake in (
                ('topRow', 'top_row'), ('leftColumn', 'left_column'),
                ('bottomRow', 'bottom_row'), ('rightColumn', 'right_column'))))
            return {**result, 'cellsPromoted': result['cells_promoted']}
        return await write(request, apply, 'prioritize')

    async def post_machine(request):
        action = request.path_params['action']
        return await write(request, lambda _: controls.machine(action, request.path_params['hid']), f'{action}_machine')

    async def post_announce(request):
        return await write(request, lambda b: controls.announce(b.get('text')), 'announce')

    chat = OpenClawChat()

    async def post_chat(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        return await chat.handle(request)

    async def post_fleet_recommendations(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        body = await _json_body(request)
        request_id = body.get("requestId")
        if body.get("schemaVersion") != 1:
            return JSONResponse({"error": "schemaVersion must be 1"}, status_code=400)
        if not isinstance(request_id, str) or not request_id:
            return JSONResponse(
                {"error": "requestId must be a non-empty string"}, status_code=400
            )
        if not recommendation_enabled:
            return JSONResponse(
                {"schemaVersion": 1, "requestId": request_id, "status": "disabled",
                 "error": "fleet recommendations are disabled"},
                status_code=503,
            )
        terrain = body.get("terrain")
        budget = body.get("budget")
        if not isinstance(terrain, dict) or not isinstance(budget, dict):
            return JSONResponse(
                {"error": "terrain and budget are required"}, status_code=400
            )
        try:
            costs = FleetCostConfig.from_mapping(os.environ)
        except ValueError as error:
            return JSONResponse(
                {"schemaVersion": 1, "requestId": request_id, "status": "error", "error": str(error)},
                status_code=503,
            )
        if budget.get("currency") != costs.currency:
            return JSONResponse(
                {"schemaVersion": 1, "requestId": request_id, "status": "error",
                 "error": f"budget currency must be {costs.currency}"},
                status_code=400,
            )

        integers = {
            "rows": terrain.get("rows"),
            "columns": terrain.get("columns"),
            "border": terrain.get("border", 1),
            "minObstacles": terrain.get("minObstacles", 3),
            "maxObstacles": terrain.get("maxObstacles", 5),
            "amount": budget.get("amount"),
        }
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in integers.values()
        ):
            return JSONResponse(
                {"schemaVersion": 1, "requestId": request_id, "status": "error",
                 "error": "terrain dimensions, bounds, and budget amount must be integers"},
                status_code=400,
            )
        food_ratio = terrain.get("foodRatio", 1.0)
        if isinstance(food_ratio, bool) or not isinstance(food_ratio, (int, float)):
            return JSONResponse(
                {"schemaVersion": 1, "requestId": request_id, "status": "error",
                 "error": "foodRatio must be numeric"},
                status_code=400,
            )
        try:
            seed_count = int(os.getenv("FLEET_RECOMMENDATION_SEED_COUNT", "7"))
            base_seed = int(os.getenv("FLEET_RECOMMENDATION_BASE_SEED", "42"))

            def calculate() -> dict:
                return recommend_fleet_profiles(
                    integers["rows"],
                    integers["columns"],
                    budget=integers["amount"],
                    costs=costs,
                    seed_count=seed_count,
                    base_seed=base_seed,
                    food_ratio=float(food_ratio),
                    border=integers["border"],
                    min_obstacles=integers["minObstacles"],
                    max_obstacles=integers["maxObstacles"],
                )

            result = await recommendation_service.run(calculate)
        except RecommendationBusy as error:
            return JSONResponse(
                {"schemaVersion": 1, "requestId": request_id, "status": "busy", "error": str(error)},
                status_code=429,
            )
        except asyncio.TimeoutError:
            return JSONResponse(
                {
                    "schemaVersion": 1,
                    "requestId": request_id,
                    "status": "timeout",
                    "error": "recommendation timed out; its worker may still finish internally",
                },
                status_code=504,
            )
        except ValueError as error:
            return JSONResponse(
                {"schemaVersion": 1, "requestId": request_id, "status": "error", "error": str(error)},
                status_code=400,
            )
        except Exception:
            return JSONResponse(
                {"schemaVersion": 1, "requestId": request_id, "status": "error",
                 "error": "fleet recommendation failed"},
                status_code=500,
            )
        return JSONResponse(
            {
                "schemaVersion": 1,
                "requestId": request_id,
                "status": "completed",
                **result,
            }
        )

    routes = [
        Route("/api/chat", post_chat, methods=["POST"]),
        Route("/", get_index, methods=["GET"]),
        Route("/api/config", config_endpoint, methods=["GET", "POST"]),
        Route("/api/state", get_state, methods=["GET"]),
        Route("/api/state/stream", stream_state, methods=["GET"]),
        Route("/api/field", get_field, methods=["GET"]),
        Route("/api/history", get_history, methods=["GET"]),
        Route("/api/events", get_events, methods=["GET"]),
        Route("/api/decisions", get_decisions, methods=["GET"]),
        Route("/api/runs", get_runs, methods=["GET"]),
        Route("/api/commands/{action}", post_command, methods=["POST"]),
        Route("/api/policy", post_policy, methods=["POST"]),
        Route("/api/rebalance", post_rebalance, methods=["POST"]),
        Route("/api/carts", post_cart, methods=["POST"]),
        Route("/api/prioritize", post_prioritize, methods=["POST"]),
        Route("/api/machines/{hid}/{action}", post_machine, methods=["POST"]),
        Route("/api/announce", post_announce, methods=["POST"]),
        Route(
            "/api/fleet-recommendations",
            post_fleet_recommendations,
            methods=["POST"],
        ),
    ]
    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    ]
    return Starlette(routes=routes, middleware=middleware)
