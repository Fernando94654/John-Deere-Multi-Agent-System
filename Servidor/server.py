import asyncio
import websockets
import json

GRID_ROWS = 10
GRID_COLUMNS = 10

obstacles = [
    {"row": 2, "column": 3},
    {"row": 2, "column": 4},
    {"row": 5, "column": 7},
    {"row": 6, "column": 7},
    {"row": 8, "column": 1}
]

silos =[
    {"row": 4, "column": 1},
    {"row": 2, "column": 7}
]


def initial_tractors():
    return {
        "T1": {
            "row": 0,
            "column": 0,
            "route": [{"row": 0, "column": c} for c in range(1, GRID_COLUMNS)]
        },
        "T2": {
            "row": 0,
            "column": 9,
            "route": [{"row": r, "column": 9} for r in range(1, GRID_ROWS)]
        }
    }


def initial_harvesters():
    return {
        "H1": {
            "row": 9,
            "column": 0,
            "route": [{"row": 9, "column": c} for c in range(1, 5)]
        }
    }



tractors = initial_tractors()
harvesters = initial_harvesters()


def reset_simulation_state():
    global tractors, harvesters
    tractors = initial_tractors()
    harvesters = initial_harvesters()
    print("Estado de la simulación reiniciado para la nueva conexión")


def advance_along_route(vehicle):
    if len(vehicle["route"]) == 0:
        return

    next_cell = vehicle["route"].pop(0)
    vehicle["row"] = next_cell["row"]
    vehicle["column"] = next_cell["column"]


def step_simulation():
    for tractor in tractors.values():
        advance_along_route(tractor)

    for harvester in harvesters.values():
        advance_along_route(harvester)


def build_state():
    return {
        "grid": {
            "rows": GRID_ROWS,
            "columns": GRID_COLUMNS
        },
        "obstacles": {
            "count": len(obstacles),
            "positions": [
                {"row": o["row"], "column": o["column"]}
                for o in obstacles
            ]
        },
        "silos": {
                    "count": len(silos),
                    "positions": [
                        {"row": s["row"], "column": s["column"]}
                        for s in silos
                    ]
                },
        "tractors": [
            {
                "id": vehicle_id,
                "row": data["row"],
                "column": data["column"],
                "route": data["route"]
            }
            for vehicle_id, data in tractors.items()
        ],
        "harvesters": [
            {
                "id": vehicle_id,
                "row": data["row"],
                "column": data["column"],
                "route": data["route"]
            }
            for vehicle_id, data in harvesters.items()
        ]
    }


async def send_states(websocket):
    while True:
        step_simulation()

        state = build_state()

        await websocket.send(json.dumps(state))

        await asyncio.sleep(0.2)


async def handle_client(websocket):
    print("Unity se conectó")

    reset_simulation_state()

    try:
        await send_states(websocket)

    except websockets.exceptions.ConnectionClosed:
        print("Unity se desconectó")


async def main():
    async with websockets.serve(handle_client, "localhost", 8765):
        print("Servidor WebSocket iniciado")
        print("Esperando conexiones en ws://localhost:8765")

        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())