"""WebSocket bridge between the simulation engine and the Unity client.

Protocol
--------
Unity sends commands; the server sends states. Each connection gets its own run.

    {"command": "start",   "rows": 10, "columns": 12}
    {"command": "pause"}
    {"command": "resume"}
    {"command": "restart"}                    same field, from tick 1
    {"command": "restart", "newSeed": true}   a freshly drawn field

A message with no `command` counts as `start`, which is the handshake Unity sent
before the control buttons existed.

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
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from johndeere.config import SimulationConfig
from johndeere.simulation import Simulation
from johndeere.world.grid import OBSTACLE

MIN_SIDE = 6

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
    """Serialize one tick. `status` and `runId` are added later, when sending."""
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
    """One client's run. Commands mutate it; the writer task reads it."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.sim: Optional[Simulation] = None
        self.obstacles: list[dict] = []
        self.last_state: Optional[dict] = None

        self.rows = args.rows
        self.cols = args.cols
        self.seed = args.seed
        self.run_id = 0

        # Set while ticking.
        self.running = asyncio.Event()
        self.rebuild_pending = False

    # --- commands ---------------------------------------------------------

    def start(self, rows: int, cols: int, new_seed: bool = False) -> None:
        """Begin a run, or restart the current one, on the given field size."""
        if rows < MIN_SIDE or cols < MIN_SIDE:
            print(
                f"Asked for {rows}x{cols}, but with {self.args.border} of headland "
                f"a field smaller than {MIN_SIDE}x{MIN_SIDE} has no crop; ignored"
            )
            return

        self.rows, self.cols = rows, cols

        if new_seed:
            self.seed = None

        # The rebuild happens in the writer task, so a run is never swapped out
        # from under a half-sent state.
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
        """Drop the current run and build a fresh one on the stored settings."""
        if self.seed is None:
            self.seed = random.randrange(2**31)

        self.sim = Simulation(build_config(self.args, self.rows, self.cols, self.seed))
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


# --------------------------------------------------------------------------
# Reading: commands from Unity
# --------------------------------------------------------------------------

def apply_command(session: Session, message: dict) -> None:
    """Route one command onto the session. Unknown commands are ignored."""
    command = message.get("command", "start")

    if command in ("start", "restart"):
        session.start(
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
    concurrent sends, so every send goes through the writer task instead.
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
# Writing: one state per tick
# --------------------------------------------------------------------------

async def send_state(websocket, session: Session, status: str) -> None:
    """Send the session's current state, stamped with status and run id."""
    state = dict(session.last_state)
    state["status"] = status
    state["runId"] = session.run_id
    await websocket.send(json.dumps(state))


async def stream_states(websocket, session: Session) -> None:
    """The only writer. Ticks the run forward and publishes each new state."""
    while True:
        await wait_while_idle(websocket, session)

        if session.rebuild_pending:
            session.rebuild_pending = False
            session.rebuild()

        if session.finished:
            await publish_final_state(websocket, session)
        else:
            snapshot = session.sim.step()
            session.last_state = build_state(
                session.sim, snapshot, session.obstacles, session.args.delay
            )
            await send_state(websocket, session, "running")

        await asyncio.sleep(session.args.delay)


async def wait_while_idle(websocket, session: Session) -> None:
    """Block until the run is started or resumed, announcing the pause once."""
    if session.running.is_set():
        return

    # Re-send the whole last state rather than a bare status message.
    # Before the first run there is nothing to re-send, so nothing goes out.
    if session.last_state is not None:
        await send_state(websocket, session, "paused")

    await session.running.wait()


async def publish_final_state(websocket, session: Session) -> None:
    """Keep the finished field on screen while the presenter talks."""
    # A field with nothing reachable to cut finishes before the first tick.
    if session.last_state is None:
        session.last_state = build_state(
            session.sim, session.sim.step(), session.obstacles, session.args.delay
        )

    await send_state(websocket, session, "finished")


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

async def handle_client(websocket, args: argparse.Namespace) -> None:
    """Serve one client with a reader and a writer running side by side."""
    session = Session(args)
    print("Client connected. Waiting for the run to be started...")

    reader = asyncio.create_task(read_commands(websocket, session))
    writer = asyncio.create_task(stream_states(websocket, session))

    try:
        done, pending = await asyncio.wait(
            {reader, writer}, return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()

        # Surface a real crash instead of letting the connection die silently.
        for task in done:
            task.result()
    except websockets.exceptions.ConnectionClosed:
        print("Client disconnected")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serves the multi-agent simulation to Unity over WebSocket."
    )
    # Unity draws each cell 20 world units wide, so the engine's default field
    # falls outside the framing of the presentation cameras. These fit onscreen.
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
    return parser.parse_args(argv)


async def main() -> None:
    args = parse_args()

    # Fail here rather than inside a connection handler, where the traceback
    # would reach Unity as an unexplained disconnect.
    build_config(args, args.rows, args.cols, args.seed).validate()

    async def handler(websocket, *_):
        await handle_client(websocket, args)

    async with websockets.serve(handler, args.host, args.port):
        print(f"Listening on ws://{args.host}:{args.port}")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped")
