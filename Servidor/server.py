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
from typing import Optional
import uvicorn
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from johndeere.config import HARVESTER_TANK, REQUEST_THRESHOLD, SimulationConfig
from johndeere.simulation import Simulation
from johndeere.world.grid import OBSTACLE

from agent import Guard, Watcher, build_server

MIN_SIDE = 6

#: Crop each harvester needs before it ever calls a cart, and the carts appear.
MIN_CROP_PER_HARVESTER = round(REQUEST_THRESHOLD * HARVESTER_TANK)


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

        self.watcher = watcher or Watcher()
        self.guard = Guard()

        # Set while ticking.
        self.running = asyncio.Event()
        self.rebuild_pending = False

        # Swapped out on every new state, so none is missed or sent twice.
        self._changed = asyncio.Event()

    # --- commands ---------------------------------------------------------

    def start(self, rows: int, cols: int) -> None:
        """Attach to the run in progress, or build the first one at this size.

        Deliberately idempotent. A client's opening handshake is a `start`, and
        so is any message that arrives without a recognisable command, so this
        must never be the thing that tears down a campaign somebody is
        watching. Once a run exists the size arguments are ignored and the
        client simply joins it; rebuilding is what `restart` is for.
        """
        if self.sim is not None:
            if (rows, cols) != (self.rows, self.cols):
                print(
                    f"Client asked to start at {rows}x{cols}, but a run is already "
                    f"going at {self.rows}x{self.cols}; joining it instead. Send "
                    f"'restart' to build a new field."
                )
            self.running.set()
            return

        self._plan_rebuild(rows, cols)

    def restart(self, rows: int, cols: int, new_seed: bool = False) -> None:
        """Build a fresh run, at a new size if one was asked for."""
        if new_seed:
            self.seed = None
        self._plan_rebuild(rows, cols)

    def _plan_rebuild(self, rows: int, cols: int) -> None:
        """Queue a rebuild for the driver, which owns swapping the world out."""
        if rows < MIN_SIDE or cols < MIN_SIDE:
            print(
                f"Asked for {rows}x{cols}, but with {self.args.border} of headland "
                f"a field smaller than {MIN_SIDE}x{MIN_SIDE} has no crop; ignored"
            )
            return

        self.rows, self.cols = rows, cols

        # The driver owns the rebuild, so no run is swapped out mid-send.
        self.rebuild_pending = True
        self.running.set()

    def pause(self) -> None:
        self.running.clear()
        print("Paused")

    def resume(self) -> None:
        if self.sim is None:
            print("Nothing to resume; the run has to be started first")
            return

        self.running.set()
        print("Resumed")

    # --- lifecycle --------------------------------------------------------

    def rebuild(self) -> None:
        """Build a fresh run and adopt it, but only if it is worth watching.

        The candidate is built into a local first. A field that cannot be
        partitioned, or one too small for the fleet to do anything interesting
        on, must not take the running campaign down with it — so on a refusal
        the previous run keeps ticking and the reason goes to the log, the same
        way an undersized field is already handled.
        """
        seed = random.randrange(2**31) if self.seed is None else self.seed

        try:
            candidate = Simulation(
                build_config(self.args, self.rows, self.cols, seed)
            )
        except ValueError as error:
            print(f"Refused {self.rows}x{self.cols}: {error}")
            return

        shortfall = fleet_shortfall(candidate)
        if shortfall is not None:
            print(f"Refused {self.rows}x{self.cols}: {shortfall}")
            return

        self.seed = seed
        self.sim = candidate
        self.obstacles = obstacle_cells(self.sim)
        self.run_id += 1
        self.last_state = None

        print(
            f"Run {self.run_id}: field {self.rows}x{self.cols}, seed {self.seed}, "
            f"{len(self.sim.harvesters)} harvesters, {len(self.sim.carts)} carts, "
            f"{len(self.obstacles)} obstacles"
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
    command = message.get("command", "start")

    if command == "start":
        # Also the fallback for a message with no command, hence idempotent.
        session.start(
            int(message.get("rows", session.rows)),
            int(message.get("columns", session.cols)),
        )
    elif command == "restart":
        session.restart(
            int(message.get("rows", session.rows)),
            int(message.get("columns", session.cols)),
            new_seed=bool(message.get("newSeed")),
        )
    elif command == "pause":
        session.pause()
    elif command == "resume":
        session.resume()
    else:
        print(f"Unknown command: {command!r}")


async def read_commands(websocket, session: Session) -> None:
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


async def drive(session: Session) -> None:
    """Advance the run and build each new state. The only writer of the world."""
    while True:
        if not session.running.is_set():
            # A whole state, not a bare status: Unity drops anything missing the world.
            if session.last_state is not None:
                session.notify("paused")
            await session.running.wait()

        if session.rebuild_pending:
            session.rebuild_pending = False
            session.rebuild()

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
            snapshot = session.sim.step()
            session.last_state = build_state(
                session.sim, snapshot, session.obstacles, session.args.delay
            )
            session.notify("running")

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
        asyncio.create_task(read_commands(websocket, session)),
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
    return parser.parse_args(argv)


async def main() -> None:
    args = parse_args()

    # Fail here, not in a handler where the traceback reaches Unity as a disconnect.
    build_config(args, args.rows, args.cols, args.seed).validate()

    shared: Optional[Session] = None
    mcp_task = None
    if args.with_mcp:
        shared = Session(
            args,
            Watcher(
                args.wake_url,
                args.wake_token,
                agent_id=args.wake_agent,
                session_key=args.wake_session,
            ),
        )
        mcp_config = uvicorn.Config(
            build_server(shared, shared.guard).streamable_http_app(),
            host=args.host,
            port=args.mcp_port,
            log_level="warning",
            access_log=False,
        )
        mcp_task = asyncio.create_task(uvicorn.Server(mcp_config).serve())
        asyncio.create_task(drive(shared))
        print(f"MCP tools on http://{args.host}:{args.mcp_port}/mcp")
        if args.wake_url:
            print(f"Waking the supervisor at {args.wake_url}")
        else:
            print("No --wake-url: the supervisor is never woken, only asked")
        if args.autostart:
            shared.start(args.rows, args.cols)

    async def handler(websocket, *_):
        await handle_client(websocket, args, shared)

    async with websockets.serve(handler, args.host, args.port):
        print(f"Listening on ws://{args.host}:{args.port}")
        try:
            await asyncio.Future()
        finally:
            if mcp_task is not None:
                mcp_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped")
