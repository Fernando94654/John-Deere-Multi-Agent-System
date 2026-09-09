"""WebSocket bridge between the simulation engine and the Unity client.

Protocol
--------
Unity sends commands; the server sends states.

    {"command": "start",   "rows": 10, "columns": 12}
    {"command": "pause"}
    {"command": "resume"}
    {"command": "restart"}                    same field, from tick 1
    {"command": "restart", "newSeed": true}   a freshly drawn field

A message with no `command` counts as `start`, which is the handshake Unity sent
before the control buttons existed.

Two tasks carry a run: one **drives** it, advancing the tick and building the
state, and one **publishes** that state down each socket. They are separate
because a run may have more than one audience — Unity draws it, and with
`--with-mcp` an agent framework watches and steers the very same world through
`Servidor/agent`. A single ticking loop is what keeps their pictures the same.

Run it with `python Servidor/server.py`; `--help` lists the field and pacing
options.
"""

from __future__ import annotations
import argparse
import asyncio
import json
import os
import random
import sys
from collections import deque
from typing import Optional
import uvicorn
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from johndeere.config import HARVESTER_TANK, REQUEST_THRESHOLD, SimulationConfig
from johndeere.simulation import Simulation
from johndeere.world.grid import OBSTACLE

from Servidor.agent import Guard, Watcher, build_server

MIN_SIDE = 6

#: Crop each harvester needs before it ever calls a cart, and the carts appear.
MIN_CROP_PER_HARVESTER = round(REQUEST_THRESHOLD * HARVESTER_TANK)

#: Per-tick samples the web dashboard can pull back for chart backfill. At the
#: default half-second tick that is a bit under an hour of history.
HISTORY_LIMIT = 6000


def fleet_shortfall(sim: Simulation) -> Optional[str]:
    """Say why this fleet has nothing to do on this field, or `None` if it does."""
    crop = sim.food_left_reachable()
    harvesters = len(sim.harvesters)
    needed = harvesters * MIN_CROP_PER_HARVESTER
    if crop >= needed:
        return None
    return (
        f"{crop} reachable crop cells for {harvesters} harvesters. Each one needs "
        f"about {MIN_CROP_PER_HARVESTER} to fill its tank far enough to call a "
        f"cart, so at this size the carts would never leave the farm. Use a "
        f"bigger field or fewer harvesters."
    )

# --------------------------------------------------------------------------
# Serialization: engine objects into the JSON Unity reads
# --------------------------------------------------------------------------

def cells_to_json(cells) -> list[dict]:
    """Turn an iterable of (row, col) tuples into Unity's {row, column} objects."""
    return [{"row": row, "column": col} for row, col in cells]


def obstacle_cells(sim: Simulation) -> list[dict]:
    """Rock cells, read once from the pristine grid: they never change."""
    return cells_to_json(
        (row, col)
        for row, values in enumerate(sim.initial_grid)
        for col, value in enumerate(values)
        if value == OBSTACLE
    )


def flatten_crop(sim: Simulation) -> list[int]:
    """The live terrain as one row-major array: -1 rock, 0 cut, 1 standing crop.

    `Field.harvest()` mutates the grid in place, so this already reflects every
    cell cut so far. Sending the whole layer each tick, rather than an initial
    grid plus per-tick deltas, keeps the client idempotent: a dropped or late
    message cannot leave Unity's picture permanently out of sync.
    """
    return [value for row in sim.field.grid for value in row]


def vehicle_json(view, agent) -> dict:
    """One machine, in the shape Unity's `VehicleData` expects.

    `view` carries the snapshot fields; `agent` is the live object, which is
    where the remaining route lives.
    """
    return {
        "id": view.label,
        "row": view.position[0],
        "column": view.position[1],
        "route": cells_to_json(agent.route),
        "state": view.state,
        "load": view.load,
        "capacity": view.capacity,
        "heading": {"row": view.heading[0], "column": view.heading[1]},
    }


def build_state(sim: Simulation, snapshot, obstacles: list[dict], delay: float) -> dict:
    """Serialize one tick. `status`, `runId` and `narration` are added on sending."""
    metrics = snapshot.metrics
    return {
        "tick": snapshot.tick,
        # Seconds until the next state arrives.
        "tickInterval": delay,
        "grid": {
            "rows": sim.config.rows,
            "columns": sim.config.cols,
        },
        "crop": {
            "cells": flatten_crop(sim),
        },
        # Owner per cell, or -1. Flat like the crop layer; Unity tints the ground with it.
        "zones": {
            "cells": sim.zone_map(),
        },
        "obstacles": {
            "count": len(obstacles),
            "positions": obstacles,
        },
        "silos": {
            "count": 1,
            "positions": cells_to_json([sim.farm]),
        },
        # The engine's grain carts are what Unity draws with its tractor prefab.
        "tractors": [
            vehicle_json(view, cart)
            for view, cart in zip(snapshot.carts, sim.carts)
        ],
        "harvesters": [
            vehicle_json(view, harvester)
            for view, harvester in zip(snapshot.harvesters, sim.harvesters)
        ],
        "metrics": {
            "harvested": metrics.harvested,
            "delivered": metrics.delivered,
            "inTransit": metrics.in_transit,
            "stranded": metrics.stranded,
            "distance": metrics.distance,
            "fuel": round(metrics.fuel, 2),
            "co2": round(metrics.co2, 2),
        },
    }


# --------------------------------------------------------------------------
# Session: one run, and the flags that steer it
# --------------------------------------------------------------------------

def build_config(args: argparse.Namespace, rows: int, cols: int, seed: Optional[int]) -> SimulationConfig:
    return SimulationConfig(
        rows=rows,
        cols=cols,
        harvesters=args.harvesters,
        carts=args.carts,
        seed=seed,
        border=args.border,
        min_obstacles=args.min_obstacles,
        max_obstacles=args.max_obstacles,
    )


class Session:
    """One run. Commands mutate it, the driver advances it, publishers read it."""

    def __init__(self, args: argparse.Namespace, watcher: Optional[Watcher] = None):
        self.args = args
        self.sim: Optional[Simulation] = None
        self.obstacles: list[dict] = []
        self.last_state: Optional[dict] = None
        self.status = "idle"
        #: The supervisor's last word, captioned under the field in Unity.
        self.narration: Optional[dict] = None

        self.rows = args.rows
        self.cols = args.cols
        self.seed = args.seed
        self.run_id = 0

        # Run parameters the web dashboard may retune. They start from the CLI
        # flags; POST /api/config swaps them out and queues a rebuild. The Unity
        # bridge only ever changed rows and columns, so these lived on `args`.
        self.n_harvesters = args.harvesters
        self.n_carts = args.carts
        self.min_obstacles = args.min_obstacles
        self.max_obstacles = args.max_obstacles

        # What the dashboard reads that Unity does not: a rolling per-tick sample
        # for the charts, a per-machine tally of time spent in each state, the
        # crop standing when the run began (for a "field %" figure), and a
        # summary of every finished run for side-by-side comparison.
        self.history: deque[dict] = deque(maxlen=HISTORY_LIMIT)
        self.state_ticks: dict[str, dict[str, int]] = {}
        self.initial_crop = 0
        self.runs: list[dict] = []

        self.watcher = watcher or Watcher()
        self.guard = Guard()

        # Set while ticking.
        self.running = asyncio.Event()
        self.rebuild_pending = False

        # Swapped out on every new state, so none is missed or sent twice.
        self._changed = asyncio.Event()

    # --- commands ---------------------------------------------------------

    def start(self, rows=None, cols=None, **changes) -> dict:
        """Start explicitly; joining an existing run never rebuilds it."""
        if self.sim is not None:
            new_seed = changes.pop('new_seed', False)
            requested = self.parameters(rows=rows, cols=cols, **changes)
            if new_seed or requested != self.parameters():
                raise ValueError('a run already exists; use restart_run or POST /api/commands/restart to change its configuration')
            self.resume()
            return self.parameters()
        return self.restart(rows, cols, **changes)

    def parameters(self, **changes) -> dict:
        """Resolve omitted values against current settings (initially CLI defaults)."""
        values = dict(rows=self.rows, cols=self.cols, harvesters=self.n_harvesters,
                      carts=self.n_carts, min_obstacles=self.min_obstacles,
                      max_obstacles=self.max_obstacles)
        limits = dict(rows=(MIN_SIDE, 200), cols=(MIN_SIDE, 200),
                      harvesters=(1, 64), carts=(1, 6),
                      min_obstacles=(0, 40000), max_obstacles=(0, 40000))
        for key, value in changes.items():
            if key not in values:
                raise ValueError(f"unknown run parameter: {key}")
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            low, high = limits[key]
            if not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            values[key] = value
        if values['min_obstacles'] > values['max_obstacles']:
            raise ValueError("min_obstacles cannot exceed max_obstacles")
        return values

    def restart(self, rows=None, cols=None, new_seed=False, *, resume=True, **changes) -> dict:
        """Atomically validate and install a run before answering Web/MCP/Unity.

        Omitted values keep the current configuration, initially the CLI defaults.
        A failed candidate never modifies the existing field or configuration.
        """
        if not isinstance(new_seed, bool):
            raise ValueError("new_seed must be a boolean")
        values = self.parameters(rows=rows, cols=cols, **changes)
        seed = random.randrange(2**31) if new_seed or self.seed is None else self.seed
        candidate = Simulation(SimulationConfig(**values, seed=seed, border=self.args.border))
        shortfall = fleet_shortfall(candidate)
        if shortfall:
            raise ValueError(shortfall)
        initial_crop = candidate.food_left_reachable()
        snapshot = candidate.step()
        if self.sim is not None and self.sim.tick > 0:
            self.runs.append(self.run_summary())
        self.rows, self.cols = values['rows'], values['cols']
        self.n_harvesters, self.n_carts = values['harvesters'], values['carts']
        self.min_obstacles, self.max_obstacles = values['min_obstacles'], values['max_obstacles']
        self.seed, self.sim = seed, candidate
        self.obstacles = obstacle_cells(candidate)
        self.run_id += 1
        self.rebuild_pending = False
        self.narration = None
        self.initial_crop = initial_crop
        self.history.clear()
        self.state_ticks = {agent.label: {} for agent in candidate.agents}
        self.last_state = build_state(candidate, snapshot, self.obstacles, self.args.delay)
        self.record_history()
        if resume:
            self.running.set()
        else:
            self.running.clear()
        self.notify("running" if resume else "paused")
        print(f"Run {self.run_id}: field {self.rows}x{self.cols}, "
              f"{self.n_harvesters} harvesters, {self.n_carts} carts")
        return values

    def reset(self) -> dict:
        """Rebuild the same configuration and seed, holding the opening frame."""
        return self.restart(resume=False)

    def pause(self) -> None:
        self.running.clear()
        self.notify("paused" if self.sim is not None else "idle")

    def resume(self) -> None:
        if self.sim is None:
            raise ValueError("no run in progress; start or restart first")
        self.running.set()
        self.notify("running")

    # --- lifecycle --------------------------------------------------------

    def _config(self, seed: Optional[int]) -> SimulationConfig:
        """The config for the next rebuild, from the current (web-tunable) knobs."""
        return SimulationConfig(
            rows=self.rows,
            cols=self.cols,
            harvesters=self.n_harvesters,
            carts=self.n_carts,
            seed=seed,
            border=self.args.border,
            min_obstacles=self.min_obstacles,
            max_obstacles=self.max_obstacles,
        )

    def run_summary(self) -> dict:
        """One finished (or in-flight) run boiled down to the headline numbers."""
        sim = self.sim
        metrics = sim._metrics()
        return {
            "runId": self.run_id,
            "ticks": sim.tick,
            "completed": sim.finished(),
            # From the run itself, not the knobs — those may already hold the
            # next run's values when this summary is taken at rebuild time.
            "rows": sim.field.rows,
            "cols": sim.field.cols,
            "harvesters": len(sim.harvesters),
            "carts": len(sim.carts),
            "seed": self.seed,
            "utilization": round(1 - sim.idle_ratio, 4),
            "harvested": metrics.harvested,
            "delivered": metrics.delivered,
            "stranded": metrics.stranded,
            "distance": metrics.distance,
            "fuel": round(metrics.fuel, 2),
            "co2": round(metrics.co2, 2),
            "fuelPerUnit": round(metrics.fuel_per_unit, 4),
            "collisions": sim.collisions,
            "rebalances": sim.rebalances,
        }

    def record_history(self) -> None:
        """Append one compact per-tick sample and update the state tally.

        Called once per advanced tick by the driver, so the dashboard's charts
        are a straight read of this buffer rather than anything recomputed.
        """
        sim = self.sim
        if sim is None:
            return
        for harvester in sim.harvesters:
            bucket = self.state_ticks.setdefault(harvester.label, {})
            state = harvester.state.value
            bucket[state] = bucket.get(state, 0) + 1
        metrics = sim._metrics()
        self.history.append(
            {
                "tick": sim.tick,
                "utilization": round(1 - sim.idle_ratio, 4),
                "harvested": metrics.harvested,
                "delivered": metrics.delivered,
                "inTransit": metrics.in_transit,
                "stranded": metrics.stranded,
                "fuel": round(metrics.fuel, 2),
                "co2": round(metrics.co2, 2),
                "distance": metrics.distance,
                "cropLeft": sim.food_left_reachable(),
                "openRequests": len(sim.dispatcher.pending),
                "collisions": sim.collisions,
                "trafficRefusals": sim.traffic.refusals,
                "tanks": [h.load for h in sim.harvesters],
                "cartLoads": [c.load for c in sim.carts],
            }
        )

    @property
    def finished(self) -> bool:
        return self.sim.finished() or self.sim.tick >= self.sim.config.max_ticks

    def notify(self, status: str) -> None:
        """Publish the current state to whoever is listening."""
        self.status = status
        waiting, self._changed = self._changed, asyncio.Event()
        waiting.set()

    async def wait_for_state(self) -> None:
        await self._changed.wait()


# --------------------------------------------------------------------------
# Reading: commands from Unity
# --------------------------------------------------------------------------

def apply_command(session: Session, message: dict) -> None:
    """Route one command onto the session. Unknown commands are ignored."""
    from Servidor.controls import RunControls
    command = message.get("command")
    if command is None:
        return  # Connecting/subscribing is not an instruction to start a run.
    RunControls(session).command(command, {k: v for k, v in message.items() if k != "command"})


async def read_commands(websocket, session: Session, *, shared_viewer: bool = False) -> None:
    """Receive commands for as long as the client is connected.

    This task never writes to the socket: `websockets` gives no guarantee for
    concurrent sends, so every send goes through the publisher task instead.
    """
    async for raw in websocket:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as error:
            print(f"Invalid JSON ({error}); ignored")
            continue

        if not isinstance(message, dict):
            print("Expected a JSON object; ignored")
            continue

        # Unity sends start automatically when its scene connects. In shared
        # mode that is a subscription, not permission to start/resume the field.
        if shared_viewer and message.get("command", "start") == "start":
            continue
        try:
            apply_command(session, message)
        except (ValueError, TypeError) as error:
            print(f"Malformed command ({error}); ignored")


# --------------------------------------------------------------------------
# Driving: one tick at a time
# --------------------------------------------------------------------------

#: Wake calls in flight, held so the loop cannot collect them early.
_wakes: set[asyncio.Task] = set()


def _wake_done(task: asyncio.Task) -> None:
    """Drop a finished wake, and say so if it failed rather than swallowing it."""
    _wakes.discard(task)
    if not task.cancelled() and task.exception() is not None:
        print(f"Waking the supervisor failed: {task.exception()}")


def _emit(session: Session, status: str) -> None:
    """Step the world once, build the new state and publish it."""
    snapshot = session.sim.step()
    session.last_state = build_state(
        session.sim, snapshot, session.obstacles, session.args.delay
    )
    session.record_history()
    session.notify(status)


async def drive(session: Session) -> None:
    """Advance ticks on the same event loop as the synchronous control methods."""
    while True:
        if not session.running.is_set():
            # A whole state, not a bare status: Unity drops anything missing the world.
            if session.last_state is not None:
                session.notify("paused")
            await session.running.wait()
            continue

        if session.sim is None:
            # The first build was refused: wait for a good start rather than crash.
            session.running.clear()
            continue

        if session.finished:
            # Keep republishing so the finished field stays on screen.
            if session.last_state is None:
                snapshot = session.sim.step()
                session.last_state = build_state(
                    session.sim, snapshot, session.obstacles, session.args.delay
                )
            session.notify("finished")
        else:
            _emit(session, "running")

            events = session.watcher.scan(session.sim)
            if events:
                for event in events:
                    print(f"  tick {event.tick}: [{event.kind}] {event.detail}")
                # Held until done: a bare task can be collected mid-flight, errors and all.
                task = asyncio.create_task(session.watcher.wake(events, session.sim))
                _wakes.add(task)
                task.add_done_callback(_wake_done)

        await asyncio.sleep(session.args.delay)


# --------------------------------------------------------------------------
# Publishing: one state per tick, per client
# --------------------------------------------------------------------------

async def publish(websocket, session: Session) -> None:
    """Send every state the driver produces down this one socket."""
    while True:
        await session.wait_for_state()
        if session.last_state is None:
            continue
        state = dict(session.last_state)
        state["status"] = session.status
        state["runId"] = session.run_id
        state["narration"] = session.narration or {"text": "", "tick": 0}
        await websocket.send(json.dumps(state))


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

async def handle_client(websocket, args: argparse.Namespace, shared: Optional[Session]) -> None:
    """Serve one client. With `--with-mcp` everyone shares the one run."""
    session = shared if shared is not None else Session(args)
    print("Client connected. Waiting for the run to be started...")

    tasks = [
        asyncio.create_task(read_commands(websocket, session, shared_viewer=shared is not None)),
        asyncio.create_task(publish(websocket, session)),
    ]
    # A private session needs its own driver; a shared one is already ticking.
    if shared is None:
        tasks.append(asyncio.create_task(drive(session)))

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in pending:
            task.cancel()

        # Surface a real crash instead of letting the connection die silently.
        for task in done:
            task.result()
    except websockets.exceptions.ConnectionClosed:
        print("Client disconnected")
    except asyncio.CancelledError:
        raise
    finally:
        for task in tasks:
            task.cancel()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serves the multi-agent simulation to Unity over WebSocket."
    )
    # Unity draws 20 world units per cell, so these fit the presentation cameras.
    parser.add_argument("--rows", type=int, default=10)
    parser.add_argument("--cols", type=int, default=12)
    parser.add_argument("--harvesters", type=int, default=2)
    parser.add_argument("--carts", type=int, default=2)
    parser.add_argument("--border", type=int, default=1)
    parser.add_argument("--min-obstacles", type=int, default=3)
    parser.add_argument("--max-obstacles", type=int, default=5)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.5, help="seconds per tick")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8765)

    supervision = parser.add_argument_group("supervision (OpenClaw / MCP)")
    supervision.add_argument(
        "--with-mcp",
        action="store_true",
        help="expose the run as MCP tools, and let every client share one world",
    )
    supervision.add_argument("--mcp-port", type=int, default=8766)
    supervision.add_argument(
        "--wake-url",
        default=None,
        help="gateway hook to POST events to, e.g. http://localhost:18789/hooks/wake",
    )
    supervision.add_argument("--wake-token", default=None, help="bearer token for it")
    supervision.add_argument(
        "--wake-agent",
        default="farm-manager",
        help="agent id the wake is routed to (for /hooks/agent)",
    )
    supervision.add_argument(
        "--wake-session",
        default="harvest",
        help="session key the woken turns share, so the agent keeps its memory",
    )
    supervision.add_argument(
        "--autostart",
        action="store_true",
        help="build the run at startup instead of waiting for a client to ask",
    )

    web = parser.add_argument_group("web dashboard")
    web.add_argument(
        "--web-port",
        type=int,
        default=0,
        help="serve the HTML dashboard and JSON API on this port; 0 (default) "
        "is off. Like --with-mcp, turning it on makes every client share one world",
    )
    web.add_argument(
        "--web-token",
        default=None,
        help="bearer token required for the mutating (POST) web routes",
    )
    return parser.parse_args(argv)


async def main() -> None:
    args = parse_args()

    # Fail here, not in a handler where the traceback reaches Unity as a disconnect.
    build_config(args, args.rows, args.cols, args.seed).validate()

    web_enabled = bool(args.web_port and args.web_port > 0)

    # Either extra surface — MCP or the web dashboard — needs one canonical run
    # for every client to watch, so it also flips the bridge into shared-world
    # mode. The lone driver task lives here, not per connection.
    shared: Optional[Session] = None
    side_tasks: list[asyncio.Task] = []
    if args.with_mcp or web_enabled:
        watcher = (
            Watcher(
                args.wake_url,
                args.wake_token,
                agent_id=args.wake_agent,
                session_key=args.wake_session,
            )
            if args.with_mcp
            else Watcher()
        )
        shared = Session(args, watcher)
        side_tasks.append(asyncio.create_task(drive(shared)))

    if args.with_mcp:
        mcp_config = uvicorn.Config(
            build_server(shared, shared.guard).streamable_http_app(),
            host=args.host,
            port=args.mcp_port,
            log_level="warning",
            access_log=False,
        )
        side_tasks.append(asyncio.create_task(uvicorn.Server(mcp_config).serve()))
        print(f"MCP tools on http://{args.host}:{args.mcp_port}/mcp")
        if args.wake_url:
            print(f"Waking the supervisor at {args.wake_url}")
        else:
            print("No --wake-url: the supervisor is never woken, only asked")

    if web_enabled:
        from Servidor.web import build_web_app  # local: starlette is only needed here

        web_config = uvicorn.Config(
            build_web_app(shared, token=args.web_token),
            host=args.host,
            port=args.web_port,
            log_level="warning",
            access_log=False,
        )
        side_tasks.append(asyncio.create_task(uvicorn.Server(web_config).serve()))
        print(f"Web dashboard on http://{args.host}:{args.web_port}")

    if shared is not None and args.autostart:
        shared.start(args.rows, args.cols)

    async def handler(websocket, *_):
        await handle_client(websocket, args, shared)

    async with websockets.serve(handler, args.host, args.port):
        print(f"Listening on ws://{args.host}:{args.port}")
        try:
            await asyncio.Future()
        finally:
            for task in side_tasks:
                task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped")
