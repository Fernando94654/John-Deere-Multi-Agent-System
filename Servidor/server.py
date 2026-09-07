"""WebSocket bridge between the simulation engine and the Unity client.

The engine in `johndeere/` never draws anything: it advances one tick at a time
and returns an immutable `Snapshot`. This server drives that loop and pushes the
resulting world state to Unity as JSON, one message per tick.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Optional

import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from johndeere.config import SimulationConfig
from johndeere.simulation import Simulation
from johndeere.world.grid import OBSTACLE


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

    `heading` matters more than it looks: an agent stands still on roughly half
    the ticks (waiting for a cart, transferring, unloading), so a client that
    infers facing from consecutive positions has nothing to work with on those
    ticks. Sending it makes the spout rotation visible too.
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
    """Serialize one tick into the JSON contract the Unity client reads."""
    metrics = snapshot.metrics
    return {
        "tick": snapshot.tick,
        # Seconds until the next state arrives. The client interpolates a move
        # over exactly this long, so one cell of travel lands right as the next
        # message does, whatever the cell size on its side.
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


def build_config(args: argparse.Namespace, rows: int = None, cols: int = None) -> SimulationConfig:
    return SimulationConfig(
        rows=args.rows if rows is None else rows,
        cols=args.cols if cols is None else cols,
        harvesters=args.harvesters,
        carts=args.carts,
        seed=args.seed,
        border=args.border,
        min_obstacles=args.min_obstacles,
        max_obstacles=args.max_obstacles,
    )


# The engine only demands 3x3, but `border` cells of headland are bare on every
# side, so a field that small has no crop at all and the run ends instantly.
MIN_SIDE = 6


async def read_field_size(websocket, args: argparse.Namespace) -> tuple[int, int]:
    """Wait for the client to say how big the field should be.

    Unity sends `{"rows": R, "columns": C}` when the operator presses Start, not
    when the socket opens, so this waits as long as it takes rather than timing
    out. Nothing is simulated until it arrives: the run has to begin on the size
    the operator chose.
    """
    print("Esperando a que el cliente inicie la simulacion...")

    try:
        raw = await websocket.recv()
        config = json.loads(raw)
        rows, cols = int(config["rows"]), int(config["columns"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        print(f"Configuracion invalida ({error}); se usan los valores del CLI")
        return args.rows, args.cols

    if rows < MIN_SIDE or cols < MIN_SIDE:
        print(
            f"El cliente pidio {rows}x{cols}, pero con {args.border} de headland "
            f"un campo menor a {MIN_SIDE}x{MIN_SIDE} se queda sin cultivo; "
            "se usan los valores del CLI"
        )
        return args.rows, args.cols

    print(f"El cliente pidio un campo de {rows}x{cols}")
    return rows, cols


async def run_simulation(websocket, args: argparse.Namespace) -> None:
    """Run one campaign for one client, pushing a state per tick."""
    rows, cols = await read_field_size(websocket, args)
    sim = Simulation(build_config(args, rows, cols))
    obstacles = obstacle_cells(sim)

    print(
        f"Cliente conectado: campo {sim.config.rows}x{sim.config.cols}, "
        f"{len(sim.harvesters)} cosechadoras, {len(sim.carts)} carros, "
        f"{len(obstacles)} obstaculos"
    )

    last_state: Optional[str] = None

    while not sim.finished() and sim.tick < sim.config.max_ticks:
        snapshot = sim.step()
        last_state = json.dumps(build_state(sim, snapshot, obstacles, args.delay))
        await websocket.send(last_state)
        await asyncio.sleep(args.delay)

    # A field with nothing reachable to cut finishes before the first tick, so
    # the loop above never runs and Unity would receive no grid at all.
    if last_state is None:
        last_state = json.dumps(build_state(sim, sim.step(), obstacles, args.delay))

    print(f"Simulacion terminada en el tick {sim.tick}")

    # The run is over but the presenter may still be talking. Keep republishing
    # the final state so the field stays on screen instead of freezing on
    # whatever happened to arrive last.
    while True:
        await websocket.send(last_state)
        await asyncio.sleep(args.delay)


async def handle_client(websocket, args: argparse.Namespace) -> None:
    try:
        await run_simulation(websocket, args)
    except websockets.exceptions.ConnectionClosed:
        print("Cliente desconectado")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sirve la simulacion multiagente a Unity por WebSocket."
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
    parser.add_argument("--delay", type=float, default=0.5, help="segundos por tick")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


async def main() -> None:
    args = parse_args()

    # Fail here rather than inside a connection handler, where the traceback
    # would reach Unity as an unexplained disconnect.
    build_config(args).validate()

    async def handler(websocket, *_):
        await handle_client(websocket, args)

    async with websockets.serve(handler, args.host, args.port):
        print(f"Servidor escuchando en ws://{args.host}:{args.port}")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServidor detenido")
