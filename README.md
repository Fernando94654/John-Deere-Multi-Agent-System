# John-Deere-Multi-Agent-System

A multi-agent simulation of a harvest campaign: several **harvesters** sweep a field and
hand their grain to several mobile **grain carts**, which ferry it to the **farm**. The
engine is pure standard library; `matplotlib` is only needed by the 2D view, and
`websockets` only by the Unity bridge (§10).

```bash
python3 main.py --frontend visual --harvesters 3 --carts 3 --seed 42 --interval 125
python3 main.py --harvesters 3 --carts 2 --seed 42
python3 main.py --frontend visual --harvesters 3 --carts 2 --save run.gif

python3 Servidor/server.py            # stream a run to Unity over WebSocket
```

---

## 1. File layout

```
main.py                       CLI: builds the config and picks a frontend
johndeere/
  config.py                   hardcoded machine specs + SimulationConfig
  world/
    grid.py                   cell values and map generation
    field.py                  Field: the queryable, mutable world
  planning/
    pathfinding.py            a_star, bfs_distances, reachable_cells
    partition.py              splitting the field into work zones
    coverage.py               sweep order inside a zone
  agents/
    base.py                   Agent: position, heading, route, fuel
    harvester.py              Harvester and its state machine
    grain_cart.py             GrainCart and its state machine
  coordination/
    dispatcher.py             auction for unload requests
    traffic.py                cell reservations
  metrics.py                  fleet counters
  simulation.py               the tick loop and the Snapshot
frontends/
  replay.py                   rebuilding the field tick by tick
  console.py                  ASCII render + ANSI animation
  visual.py                   2D animation with matplotlib
Servidor/
  server.py                   WebSocket bridge to the Unity client (§10)
  agent/                      MCP supervision layer (§11)
    tools.py                  what the supervisor may read and change
    policy.py                 the gate every command passes, and the audit trail
    watcher.py                what wakes the agent when the fleet gets stuck
    mcp.py                    MCP over JSON-RPC
    http.py                   just enough HTTP to carry it
agent/
  skills/farm-manager/        the OpenClaw skill: doctrine, not capability
  openclaw.example.json5      MCP registration, model backends, chat channel
  run-demo.sh                 brings up the gateway and the simulation together
  orin-model.sh               runs the local model in a container on the Jetson
```

Dependencies run one way only:

```
world  ->  planning  ->  agents  ->  coordination  ->  simulation  ->  frontends
                                                                 \->  Servidor
                                                                       \->  agent
```

**The engine never prints or draws.** Frontends observe a run through an immutable
`Snapshot` per tick, which is the only contract between the two sides. The Unity bridge
is just another consumer of that same `Snapshot`, over a socket instead of a screen.

---

## 2. The world

### 2.1 Cell values (`world/grid.py`)

| Value | Meaning |
|-------|---------|
| `-1` | Obstacle: **impassable** to everything |
| `0` | Bare ground: cut, headland, or the farmyard |
| `1` | Standing crop: one grain unit when cut |

### 2.2 Map generation

`generate_grid(rows, cols, food_ratio, min_obstacles, max_obstacles, seed, farm, border)`
builds the field from **hard constraints**, not from per-cell probabilities:

1. **Headland** (`border`, default 1): the outer ring never gets crop or rocks. It is the
   fleet's ring road. Without it, on a fully sown field, a cart can only stand on the farm
   at tick 1; with it, on the 60-80 cells of the ring.
2. **Obstacles**: a count is drawn from `[min_obstacles, max_obstacles]` and placed inside
   the headland.
3. **Crop**: `round(food_ratio × sowable)`, where `sowable` = interior − obstacles − farm.
   That is why **`--food-ratio 1` sows everything that is not rock, headland or farm**,
   whatever the obstacle draw turns out to be.

The farm sits at `(0, 0)`, on the headland, and always stays clear.

### 2.3 `Field` — the queryable world (`world/field.py`)

The key point is that **the two classes of machine see a different field**:

```python
field.drivable(cell, avoid_crop=False)   # harvester: anything that is not a rock
field.drivable(cell, avoid_crop=True)    # cart: and no standing crop either
```

A cart would flatten the crop, so it only drives on ground that has already been cut, the
headland and the farm. **Its drivable map grows during the campaign** as the harvesters
open a way through. Everything else (`neighbors`, `a_star`, `bfs_distances`) inherits that
distinction through the `Agent.blocked_by_crop` attribute.

### 2.4 Geometry of the left-hand side

A harvester unloads over a spout on its **left**, so it matters which way it faces. With
rows growing downwards, the left of heading `(dr, dc)` is `(-dc, dr)`:

| Heading | Left | Docking cell |
|---|---|---|
| north `(-1,0)` | west | `(r, c-1)` |
| south `(+1,0)` | east | `(r, c+1)` |
| east `(0,+1)` | north | `(r-1, c)` |
| west `(0,-1)` | south | `(r+1, c)` |

`heading_for_dock(position, dock)` inverts the relation: it returns the heading that leaves
`dock` on the left, which is the direction towards `dock` turned to the right.

---

## 3. The algorithms

### 3.1 A* with obstacles (`planning/pathfinding.py`)

`a_star(field, start, goal, blocked=(), avoid_crop=False)` — 4-neighbourhood, Manhattan
heuristic (admissible because there are no diagonals and no variable costs), priority queue.

- `blocked`: cells to treat as impassable **for this search only**. It is how an agent
  plans a detour around another machine.
- `avoid_crop`: restricts the search to ground a cart is allowed to drive on.

`bfs_distances(field, source, avoid_crop, blocked)` gives the full distance map; it backs
the auction bids, the partition seeds and the choice of docking cell. `reachable_cells` is
its key set.

**Unreachable crop**: any crop walled in by rocks is found with a BFS from the farm and
**excluded from the target**; otherwise the campaign could never finish. It is reported
separately in the closing summary.

### 3.2 Splitting the field into zones (`planning/partition.py`)

Splitting by columns stopped working once obstacles became solid: a column can be cut in
half by a rock and the two halves may be a long detour apart. Zones are grown **through
drivable adjacency instead**, so the detour is priced in by construction.

1. **Farthest-point sampling for the seeds**: the first is the cell farthest from the farm;
   each further seed maximises its driving distance to the seeds already chosen. Harvesters
   start spread out instead of piled into one corner.
2. **Round-robin growth (balanced multi-source BFS)**: on every round **each** zone absorbs
   one cell from its own frontier. Since they all advance at the same rate, the sizes come
   out even.
3. **Connectivity-preserving rebalance**: a zone can get walled in by its neighbours and
   end up short. Every adjacent pair differing by two or more cells hands over a boundary
   cell, **but only if the donor zone survives as one piece** without it. Scanning all
   pairs — rather than just largest against smallest — is what lets the surplus travel
   along a chain of zones.

Invariants: zones are **connected**, disjoint, and their union is the reachable set.
Connectivity wins over perfect balance: measured over 150 maps, 77% of runs end within one
cell of perfect and the worst case was three cells out of ~350.

### 3.3 Sweep order (`planning/coverage.py`)

Inside its zone a harvester visits the crop cells in **serpentine order** (down one column,
up the next). Gaps — rocks, cells owned by another zone — are simply skipped, and A* joins
one target to the next around whatever is in the way. Crop driven over on the way is cut
all the same.

A side effect that comes for free: driving down a column the left-hand side is standing
crop, but **driving up the next one the left is the column just cut**. That is why
unloading on the go works by itself on alternating columns.

### 3.4 The grain cart auction (`coordination/dispatcher.py`)

When a harvester crosses `REQUEST_THRESHOLD` it posts an `UnloadRequest`. Every tick the
free carts bid on the open requests and the cheapest bid wins:

```
cost = length of the A* route (over cut ground)
     + 2 × (grain that does not fit)        # it would have to come back for the rest
     - 1.5 × ticks the request has waited   # nobody starves
```

Ties break on cart id, so it is deterministic. A cart with no route yet **cannot bid** — it
waits for the harvester to open one. A cart on its way home with room to spare bids too:
diverting it is cheaper than making anyone wait out the full round trip.

If a cart fills up mid-transfer the request is **reopened** rather than counted as served;
that detail was a silent deadlock.

### 3.5 Cell reservations and right of way (`coordination/traffic.py`, `simulation.py`)

Every machine that wants to move **claims** the cell it is about to enter. The claim is
granted only if nobody stands there and nobody else has claimed it this tick, which
guarantees that **no two machines ever share a cell** and that they never swap places
through each other. The farm is exempt: it is a depot where machines park and queue.

When a machine is refused three ticks running, the unjamming logic kicks in:

1. **Right of way.** There is a fixed priority order (harvesters by id, then carts). The
   higher-priority machine just waits; the lower-priority one gets out of the way. That is
   what breaks the symmetry of a head-on meeting: if both swerve at once they end up nose
   to nose again forever.
2. **...but only if the other one can actually move.** Right of way is granted only when
   the blocker *has a route* and *has a free exit*. Deferring to a machine boxed in between
   a rock and another machine left the whole queue waiting on the one that could not move.
3. **A detour that avoids every machine**, not just the one in front: a detour that swings
   into the next machine in the queue re-creates the jam one cell along. The farm and the
   goal itself stay open, because blocking the destination makes the search fail instead of
   finding the way round.
4. **Pulling over**: with no detour available, the machine steps into any free adjacent
   cell.
5. **Deferring a target** (`defer_target`, harvesters only): if the blocker is parked on the
   very cell to be cut, no detour to it can exist; that target goes to the back of the plan
   and gets picked up later.

### 3.6 Left-side docking and turning on the spot

Grain only moves when the cart occupies **exactly** the cell to the harvester's left. Since
a cart cannot drive on standing crop, that cell is often unavailable:

- `GrainCart.station()` ranks docking cells **by real driving distance** (BFS over cut
  ground), not in a straight line. The cell in front of the harvester can be a stone's
  throw away with no route to it, while the trail it just cut always leads somewhere.
- If the left-hand side cannot be reached, the cart pulls up on **any cut cell beside** the
  harvester.
- The harvester then **stops, turns on the spot at one quarter turn per tick, empties, and
  carries on**. No grain moves while it is turning: a cart arriving on the wrong side costs
  real time, and it shows up in the idle counter.
- An `unloading` latch keeps the machine stopped until the tank is empty. Without it, it
  would drive off again the moment the turn lined up, leaving the transfer half done.

If the cart **does** manage to take the left-hand cell there is no stop at all: both machines
roll on in parallel with the grain flowing (*unloading on the go*).

---

## 4. The agents

Shared base (`agents/base.py`): position, `heading`, route, `distance`, `fuel`,
`idle_ticks`. `hold(productive=True)` marks a stop that is doing work — a cart parked under
the spout is moving grain, not waiting — so it burns idle fuel without polluting the idle
metric.

### 4.1 Harvester (`agents/harvester.py`)

```
to zone ──► harvesting ──► waiting cart ──► returning ──► done
                │  ▲            │                 ▲
                ▼  │            ▼                 │
            rotating ──────► unloading ───────────┘
```

| State | What it is doing |
|---|---|
| `to zone` | Driving from the farm to its zone |
| `harvesting` | Sweeping its zone; cuts the cell it stands on |
| `waiting cart` | Tank full **with crop still to cut** and no cart alongside |
| `rotating` | Turning 90° to bring the spout round to the cart |
| `unloading` | Stopped, cart on its left, passing grain across |
| `returning` | Zone finished: driving home, with whatever is still in the tank |
| `done` | Parked at the farm, day over |

It calls for a cart at the threshold and **keeps cutting** while it waits. It only stands
still if it fills up with no cart docked *and* there is still crop to cut. Once its zone is
finished it stops waiting for anyone: it drives home and empties into the silo itself, which
is credited exactly like a cart's delivery. That trip happens either way — waiting for a
cart only added dead time to it.

### 4.2 Grain cart (`agents/grain_cart.py`)

```
idle ──► to harvester ──► transferring ──► to farm ──► unloading ──► idle
```

| State | What it is doing |
|---|---|
| `idle` | Parked at the farm, available for the auction |
| `to harvester` | Driving to the docking cell it won |
| `transferring` | Paired up: docked, or keeping station |
| `to farm` | Full, or out of work: heading back to the silo |
| `unloading` | Emptying into the farm (`UNLOAD_TICKS`) |

An idle cart **always drives home**: parked in the middle of the field it is a rock in
everyone's way, and it may be sitting on crop somebody else has to cut.

---

## 5. The tick (`simulation.py`)

The order of the phases is not decorative — each one is where it is for a reason:

1. **Requests**: harvesters over the threshold post their request.
2. **Auction**: free carts are assigned to the open requests.
3. **Transfer**: grain flows across every coupled pair. *This runs before anybody moves* so
   that a tank which hit 100% already has room by the time its harvester decides, and does
   not lose the tick.
4. **Reservations**: where everyone stands is recorded before anybody moves.
5. **Harvesters**: decide, move and cut. They go first because they hold right of way and
   because their position defines where the docking cell is.
6. **Carts**: decide and move. Going second, they already know their partner's new position
   and can keep formation within the same tick.
7. **Audit**: the two hard invariants are checked (no collision, nobody on a rock) and the
   `Snapshot` is emitted.

**End of campaign**: no reachable crop left, every harvester back at the farm with an empty
tank, and every cart empty and parked. `max_ticks` is the safety net.

---

## 6. Metrics (`metrics.py`)

Per machine: distance, fuel, idle ticks. Per fleet: harvested, delivered, in transit,
stranded (grain aboard a machine that broke down), CO₂ (`litres × 2.68`), litres per unit
delivered, and right-of-way stops.

They are what sizes the fleet, which is where the system is best seen at work. A 14×20
field on the default settings, averaged over 4 seeds:

| Fleet | Ticks | Idle |
|---|---|---|
| 1H/1C | 422 | 201 |
| 2H/1C | 376 | 497 |
| 3H/2C | 231 | 468 |
| 5H/3C | 202 | 744 |

Adding harvesters without adding carts saturates the logistics: the bottleneck moves from
cutting to hauling, and the idle time gives it away before the total time does.

---

## 7. Parameters

**From the CLI** (`main.py`): `--rows`, `--cols`, `--harvesters`, `--carts`,
`--food-ratio`, `--border`, `--min-obstacles`, `--max-obstacles`, `--seed`, `--max-ticks`,
`--frontend`, `--delay`, `--interval`, `--save`, `--fps`, `--no-animation`.

**Hardcoded** (`config.py`), picked so the logistics are visible in short runs:

| Constant | Value | What it is |
|---|---|---|
| `HARVESTER_TANK` | 20 | Harvester tank capacity |
| `CART_CAPACITY` | 60 | Cart capacity: three tanks |
| `TRANSFER_RATE` | 2 | Units moved per tick while transferring |
| `REQUEST_THRESHOLD` | 0.5 | When it calls for a cart, as a share of the tank |
| `UNLOAD_TICKS` | 3 | Ticks spent emptying at the farm |
| `FUEL_PER_CELL_*` | 0.8 / 0.5 | Litres per cell: harvester / cart |
| `CO2_KG_PER_LITRE` | 2.68 | Diesel emission factor |
| `BLOCKED_TICKS_BEFORE_REROUTE` | 3 | Patience before unjamming |
| `WAIT_WEIGHT` | 1.5 | How much waiting discounts a bid |

---

## 8. Verified invariants

Checked over 56 configurations (8 seeds × 7 fleets), on fields with and without a headland:

- The campaign always **finishes**, and `harvested == delivered` with no grain lost.
- **Zero collisions** and **zero machines on obstacles**.
- No cart **ever** drives on standing crop.
- **Every** cart transfer happens with the cart on the left-hand side. (A harvester that
  finished its zone empties straight into the silo, with no cart involved.)
- A* is optimal (it matches BFS) and its routes are contiguous and drivable.
- Zones are connected, disjoint, and cover everything reachable.

```bash
# no cart on standing crop
python3 -c "
from johndeere.config import SimulationConfig
from johndeere.simulation import Simulation
from johndeere.world.grid import FOOD
sim = Simulation(SimulationConfig(rows=14, cols=20, harvesters=3, carts=2, seed=0))
while not sim.finished() and sim.tick < 20000:
    sim.step()
    for c in sim.carts:
        assert sim.field.grid[c.position[0]][c.position[1]] != FOOD
print('ok')
"

# the campaign closes and the grain is conserved
python3 -c "
from johndeere.simulation import run
for seed in range(8):
    r = run(rows=14, cols=20, harvesters=3, carts=2, seed=seed, max_ticks=20000)
    assert r.completed and r.metrics.harvested == r.metrics.delivered
    assert not r.collisions and not r.obstacle_violations
print('ok')
"
```

---

## 9. Decisions and known limits

- **Zone connectivity wins over perfect balance.** Sometimes the last cell cannot be handed
  over without splitting the donor zone in two, and it is left where it is.
- **One handshake tick per docking.** The cart enters the docking cell after its harvester
  has already decided for that tick, so the transfer starts on the next one. It is
  unavoidable in a turn-based model, and it is never two ticks in a row.
- **Priority is fixed, not negotiated.** The right-of-way order is static. Auctioning the
  right of way too would be more elegant, but the static rule is already deadlock-free and
  far easier to debug.
- **A single silo, at `(0,0)`.** The farm is fixed; supporting several would mean routing
  each unload to the nearest one.
- **Soil sensors (moisture, fertility) and ripeness-based priority** are out of scope: the
  challenge brief suggests them but does not require them.
- **The fleet can grow but not shrink.** Carts are looked up by `carts[id]`, so ids must
  stay equal to the index; appending preserves that and removing does not.
- **A broken machine strands its grain.** It is reported as `stranded` rather than quietly
  credited, so `harvested == delivered` becomes `harvested == delivered + in_transit`.

---

## 10. The Unity bridge (`Servidor/server.py`)

A WebSocket server that drives the engine and streams the world to a Unity client. It is
a frontend like any other: it consumes the same `Snapshot` the console and 2D views do,
and adds nothing to `johndeere/`.

```bash
python3 Servidor/server.py                        # ws://localhost:8765
python3 Servidor/server.py --rows 14 --cols 18 --delay 0.3 --seed 42
```

Each connection gets its own `Session`: its own `Simulation`, its own seed and its own
run counter. Two tasks run side by side — one reads commands, one writes states. Every
send goes through the writer, because `websockets` gives no guarantee for concurrent
sends from two coroutines.

### 10.1 Commands (Unity → server)

| Command | Payload | Effect |
|---|---|---|
| `start` | `rows`, `columns` | **Joins the run in progress**, or builds the first one at this size |
| `pause` | — | Stops advancing ticks |
| `resume` | — | Continues from the tick where it stopped |
| `restart` | `rows`, `columns`, `newSeed` (all optional) | Rebuilds from tick 1, at a new size if asked |

```jsonc
{"command": "start",   "rows": 10, "columns": 12}
{"command": "pause"}
{"command": "resume"}
{"command": "restart"}                    // same field, from the top
{"command": "restart", "newSeed": true}   // a freshly drawn field
```

Nothing is simulated until a `start` arrives, so the operator's chosen field size is what
gets built. A message with **no** `command` counts as `start`, which is the handshake the
Unity client used before the control buttons existed.

**`start` is idempotent, and that matters.** Once a run exists it joins it and ignores the
size arguments; only `restart` rebuilds. It used to do both, which was harmless when every
connection got its own run and destructive once `--with-mcp` made the world shared: the
Unity handshake tore down the campaign the operator had just started and rebuilt it at the
client's own field size, and since a message with no command *is* a `start`, so did any
unexpected message. Tearing down a live campaign has to be something you ask for on
purpose.

The seed is pinned on the first run and reused on every restart, so Restart reproduces
the identical field — you can rehearse a demo and repeat it. `newSeed` draws a new one.

Sizes below **6×6** are refused: `border` cells of headland are bare on every side, so a
smaller field has no crop and the run would end instantly.

A field is also refused when **the fleet has nothing to do on it**: each harvester needs
roughly `REQUEST_THRESHOLD × HARVESTER_TANK` reachable crop cells before it ever fills up
far enough to call a cart. Below that the carts never leave the farm and the harvesters
carry their own grain home — every rule working exactly as written, and nothing worth
watching. Four harvesters on a 6×6 field get twelve cells between them, which is how this
check came to exist.

The candidate run is built and checked *before* it is adopted, so a refusal leaves the
campaign already on screen ticking along. Malformed JSON, unknown commands, invalid sizes
and unworkable fleets are all logged and ignored; the connection survives every one.

### 10.2 State (server → Unity)

One message per tick. Shaped for Unity's `JsonUtility`, which deserializes **neither
nested arrays nor dictionaries** — hence the flat crop array and the fixed field names.

```jsonc
{
  "tick": 1,
  "tickInterval": 0.5,              // seconds until the next state
  "status": "running",              // "running" | "paused" | "finished"
  "runId": 1,                       // increases on every restart
  "grid":      { "rows": 6, "columns": 6 },
  "crop":      { "cells": [0, 0, 0, 1, ...] },        // row-major, rows*columns long
  "zones":     { "cells": [0, 0, 1, -1, ...] },       // owning harvester per cell, -1 none
  "narration": { "text": "", "tick": 0 },             // the supervisor's last word (§11)
  "obstacles": { "count": 3, "positions": [{"row": 1, "column": 2}, ...] },
  "silos":     { "count": 1, "positions": [{"row": 0, "column": 0}] },
  "tractors": [                     // the engine's grain carts
    { "id": "C0", "row": 0, "column": 0, "route": [],
      "state": "idle", "load": 0, "capacity": 60,
      "heading": {"row": 0, "column": 1} }
  ],
  "harvesters": [
    { "id": "H0", "row": 1, "column": 0,
      "route": [{"row": 2, "column": 0}, ...],        // remaining path
      "state": "to zone", "load": 0, "capacity": 20,
      "heading": {"row": 1, "column": 0} }
  ],
  "metrics": { "harvested": 0, "delivered": 0, "inTransit": 0, "stranded": 0,
               "distance": 2, "fuel": 1.7, "co2": 4.56 }
}
```

| Field | Why it is there |
|---|---|
| `crop.cells` | `-1` rock, `0` cut, `1` standing. Sent **whole every tick** rather than as deltas, so a dropped message cannot leave the client permanently out of sync. |
| `heading` | A `(row, column)` step. An agent stands still on roughly half the ticks, so a client that infers facing from consecutive positions has nothing to work with; sending it also makes the spout rotation visible. |
| `tickInterval` | Lets the client spread one cell of travel over exactly one tick, whatever its own cell size is. |
| `runId` | The only way for the client to tell a restart from one more tick, so it knows when to clear the previous field. |
| `status` | Saves the client from inferring the run state. While paused the server re-sends the **whole** last state rather than a bare status, because the Unity client drops any message missing `grid`, `obstacles`, `silos`, `tractors` or `harvesters`. |
| `tractors` | The engine has harvesters and grain carts; `tractors` is the carts, named for the prefab Unity draws them with. |
| `zones` | Which harvester owns each cell, flat and row-major for the same reason as `crop`. It is what lets Unity tint the ground: when the supervisor redraws the zones (§11), the boundary between colours moves on screen mid-run. |
| `narration` | One line from the supervisor explaining what it just did, to caption under the field. Empty until an agent is connected. |

Coordinates are the engine's: `row` grows downward, `column` rightward, `index = row *
columns + column`. The farm at `(0, 0)` is reported as the single silo.

When the campaign ends the server keeps republishing the final state with
`status: "finished"` instead of going quiet, so the field stays on screen while the
presenter talks. A `restart` after that works normally.

### 10.3 Parameters

`--rows`, `--cols`, `--harvesters`, `--carts`, `--border`, `--min-obstacles`,
`--max-obstacles`, `--seed`, `--delay` (seconds per tick, default `0.5`), `--host`,
`--port` (default `8765`).

For the supervision layer (§11): `--with-mcp`, `--mcp-port` (default `8766`),
`--wake-url`, `--wake-token`, `--autostart`. Without `--with-mcp` nothing changes — each
connection gets its own run, exactly as before.

`--rows`/`--cols` are only the fallback: the client's `start` decides the real size. The
defaults are small because Unity draws each cell 20 world units wide, and a large field
falls outside the framing of the presentation cameras.

---

## 11. The supervision layer (`Servidor/agent/`, `agent/`)

The engine plans well and re-plans never. Zones are drawn once, in
`Simulation.__init__`, when nothing has been cut and area is a fair proxy for work.
Later in a campaign the two come apart: a harvester can own a quarter of the map with
three cells left on it while its neighbour still has forty. It drives home and parks,
and the clock keeps running for everybody else. That is what the idle column in §6 is
measuring, and no amount of better routing fixes it — it is a decision nobody was making.

This layer is where that decision lives. An agent framework — OpenClaw, or anything that
speaks MCP — watches the run and steers it **at the level of goals**. It never drives a
machine, plans a route or picks a cell to cut.

```bash
python3 Servidor/server.py --with-mcp --autostart      # ws://8765 + MCP on http://8766/mcp
python3 Servidor/server.py --with-mcp --wake-url http://localhost:18789/hooks/wake
```

### 11.1 What the engine gained

Four commands, each reusing machinery that was already here:

| Command | What it does |
|---|---|
| `rebalance()` | Redraws the zones over the crop **still standing**, seeded where the machines are now, and calls a harvester that had already parked back out |
| `disable(id)` / `repair(id)` | Breaks a machine down where it stands: it stops working, becomes an obstacle, releases its cart and hands its zone to the others |
| `prioritize(a, b)` | Brings the crop inside a rectangle to the front of every work plan |
| `add_cart()` | One more cart out of the farm |

`rebalance` is `partition_zones` again, with two changes. Seeds come from
`anchored_seeds` (each machine's nearest remaining cell) instead of `farthest_point_seeds`
— a split that ignored current positions would send the whole fleet across the field to
swap places. And the round-robin growth now advances by **one unit of weight** rather than
one cell, with `crop_only` weighing standing crop at 1 and cut ground at 0. Ground already
cut is swept up on the way to the next standing cell instead of costing anybody a turn, so
the zones come out balanced by *work* rather than by area. With the default `uniform`
weight the loop is exactly what it was, which is why the opening partition is unchanged.

`REQUEST_THRESHOLD` and `WAIT_WEIGHT` moved out of module scope into a `Policy` object the
simulation, the harvesters and the auction all share, so retuning it reaches all three at
once.

### 11.2 Two bugs this uncovered

Both were in `main` before any of this existed, and both are fixed:

- **An orphaned unload request.** A cart that fills up or loses its dock calls
  `release()` without telling the dispatcher. The `UnloadRequest` stays marked as served by
  a cart that is never coming, never returns to `pending`, and the harvester waits out the
  rest of the campaign with a full tank. `Dispatcher.sync()` now reopens those before each
  auction. Measured over 200 runs (2 field sizes × 5 fleets × 20 seeds): **2 hung, now 0.**
- **A bid that could not be placed.** `Dispatcher.bid` routed to the harvester's own cell.
  A harvester whose tank fills up on a cell it cannot then cut is standing *on standing
  crop*, which no cart may drive onto — so no cart could bid and the machine waited
  forever. The bid now prices the drive to the berth from `GrainCart.station()`, which is
  where the cart was actually going anyway.

### 11.3 The tools

Reading: `get_fleet_state` (crop left per machine, waiting time, open requests, rolling
idle ratio, policy in force), `get_field_map` (the field as text, digits showing which
harvester owns each standing cell), `explain_last_decision`, `list_recent_events`.

Changing: `rebalance_zones`, `disable_machine`, `repair_machine`, `prioritize_region`,
`set_policy`, `add_cart`, `announce`, `pause_run`, `resume_run`, `restart_run`.

`announce` captions the field with one line in the operator's language; it rides down to
Unity in the state as `narration`, so the reasoning is on screen with the thing it explains.

### 11.4 The gate (`agent/policy.py`)

NemoClaw pairs its agent with **OpenShell**, a runtime that decides what the model may
actually execute. That runtime is in preview, so the same idea is implemented here, in the
one place it can be audited: an allowlist, bounds-checked arguments, a budget of three
world-changing calls per 60 ticks *or* 30 seconds, and a log of every call including the
refused ones — which is exactly what `explain_last_decision` returns.

The dual window matters: a paused or finished run stops advancing the tick, and a budget
measured only in ticks would never refill. Three commands and the supervisor would be
locked out of its own fleet for good.

The guarantees in §8 are properties of the engine, and they stay properties of the engine
because nothing outside it can reach past this gate. Verified by hammering the commands at
random — 20 seeds, ~250 calls — and checking every invariant still holds.

### 11.5 What wakes it (`agent/watcher.py`)

OpenClaw's heartbeat runs on a clock measured in minutes; a campaign runs at half a second
per tick, so a scheduled heartbeat would sleep through the whole harvest. The gateway also
takes wakes on demand, which is the right shape: the simulation knows the moment something
goes wrong and says so, through `POST /hooks/wake`.

Four conditions, each debounced so a situation lasting two hundred ticks raises one event
rather than two hundred: a harvester pinned with a full tank, the work gone lopsided, the
fleet standing still, a machine down. They are phrased as facts, not instructions —
deciding to do nothing is a valid answer and the skill says so.

With no `--wake-url` the server never wakes anybody and answers only when asked. With a
gateway that is down, `post_json` returns 0 and the campaign carries on: **the simulation
never depends on the agent being there.**

### 11.6 Running it

Verified end to end on OpenClaw **2026.9.2**. Every step below was actually run.

**1. OpenClaw needs Node ≥22.22.3**; Ubuntu ships 18. Without root, put both in
`~/.local`:

```bash
curl -sL https://nodejs.org/dist/v24.20.0/node-v24.20.0-linux-x64.tar.xz | \
  tar -xJ -C ~/.local/node --strip-components=1        # mkdir -p it first
export PATH="$HOME/.local/node/bin:$HOME/.local/bin:$PATH"
npm config set prefix ~/.local && npm install -g openclaw
```

**2. Configure.** Copy `agent/openclaw.example.json5` into
`~/.openclaw/openclaw.json`, put the absolute path to `agent/skills` in
`skills.load.extraDirs`, and generate a hook token. Then:

```bash
openclaw config validate      # Config valid: ~/.openclaw/openclaw.json
openclaw skills list | grep farm-manager      # ✓ ready
```

Three things that will bite otherwise, all found the hard way:

- **`gateway.mode` is mandatory.** Without it the gateway refuses to start
  ("existing config is missing gateway.mode… treat this as suspicious").
- **The skill cannot be symlinked into the workspace.** OpenClaw rejects it as a
  `symlink-escape`. `skills.load.extraDirs` is the supported way to keep it
  versioned in this repo.
- **The two hooks disagree on the field name.** `/hooks/agent` runs a turn for
  one named agent and reads `message`; `/hooks/wake` queues an event for the
  main session and reads `text`. The watcher sends both, so `--wake-url` alone
  picks the behaviour. `/hooks/agent` is the one you want here — `/hooks/wake`
  would go to the default agent, not to `farm-manager`.

**3. Start it.** `agent/run-demo.sh` brings up both processes, reads the hook
token out of the config so they cannot drift apart, and stops both on Ctrl-C.
By hand it is:

```bash
python3 Servidor/server.py --with-mcp --autostart \
  --rows 16 --cols 22 --harvesters 4 --carts 2 \
  --wake-url http://127.0.0.1:18789/hooks/agent --wake-token "$TOKEN"

openclaw gateway
openclaw mcp probe johndeere        # johndeere: 14 tools
```

**4. Talk to it**, or leave it alone and let the field wake it. The four that make a
demo, in order:

```bash
A="openclaw agent --agent farm-manager --session-key harvest -m"
$A "¿Cómo va la cosecha?"
$A "Se descompuso la cosechadora H2. Reorganiza y avisa en pantalla."
$A "Viene lluvia sobre las filas 1 a 4. Prioriza esa franja."
$A "Explica qué has hecho hasta ahora y por qué."
```

Keep the same `--session-key` across all four so the agent remembers what it already did;
the last one then answers from its own history rather than from the log.

Tools carry MCP annotations (`readOnlyHint`, `destructiveHint`), so reading the
fleet state does not prompt the operator for approval while changing it does.

### 11.7 The two backends

**Claude Pro.** `models.providers.anthropic.agentRuntime.id = "claude-cli"` routes model
calls through the installed `claude` binary and its own login, so turns draw on the
subscription instead of API credits. Confirmed in the gateway log:

```
[agent/cli-backend] cli turn: provider=claude-cli model=claude-opus-5
```

**Local, on a Jetson AGX Orin.** The first version of this installed Ollama on the Orin's
host. That was the wrong call: the machine is shared, the installer left a service
`enabled` at boot, it listened on `0.0.0.0:11434` with no authentication — handing the
GPU to anyone on the tailnet — and it parked 21 GB of weights in `/usr/share/ollama`. All
of that has been removed and the host is back to what it was.

It now runs in a container, started only when it is wanted:

```bash
./agent/orin-model.sh start          # container up, GPU checked
./agent/orin-model.sh pull qwen3:8b  # weights into a named volume
./agent/orin-model.sh tunnel         # forward it to localhost:11435 here
./agent/orin-model.sh stop           # gone; the volume keeps the models
./agent/orin-model.sh purge          # volume and image gone too
```

Four properties, each deliberate on a machine somebody else also uses:

- **Nothing on the host.** One container and one named volume, both prefixed `jd-`. The
  team's own `home2-hri-ollama-*` containers and their model directory are untouched.
- **Nothing at boot.** `--rm` and no restart policy. A reboot brings nothing back and
  there is no service for anyone to discover.
- **Not on the tailnet.** The port is published on the Orin's loopback only, so it is
  reachable through an SSH tunnel and not otherwise. `curl http://<orin>:11434/api/version`
  from another machine gets nothing.
- **Removable in one command.** `purge` deletes the container, the volume and the image.

Two things worth knowing before repeating this on a Jetson:

- **JetPack 7 has no jetson-containers build.** `dustynv/ollama` stops at `r36.4.3`
  (JetPack 6) and this board runs L4T **R39.2.1**. The official `ollama/ollama` image has
  a native arm64 build and does detect the iGPU — it skips its own CUDA 12 libraries
  (`compute capability not in compiled architectures`, the Orin is `cc=870`) and picks
  the CUDA 13 ones:

  ```
  inference compute library=CUDA compute=8.7 name=CUDA0 description=Orin
    type=iGPU driver=13.2 total="61.4 GiB"
  ```

  `--runtime nvidia` on its own is enough, which is what the machine's own compose files
  use; `--gpus all` adds nothing on Tegra.

- **Ollama sizes the context off total memory.** On 61 GiB of unified memory it picks
  `default_num_ctx=262144` — a quarter-million-token KV cache for a prompt of a few
  thousand. `OLLAMA_CONTEXT_LENGTH` is pinned to 16384 in the run script.

### 11.8 The prompt is the bottleneck, and most of it is not ours

A local model pays for the whole prompt on every call, and a tool call costs a second
round trip, so one supervision decision reads the prompt twice. Measuring where those
tokens come from was more useful than any model choice:

| Part | Tokens | Share |
|---|---|---|
| The 14 MCP tool schemas | ~1,670 | 7% |
| `SKILL.md` | ~1,050 | 4% |
| **Everything OpenClaw injects** | **~22,100** | **89%** |
| Total measured on the wire | 24,853 | |

So the obvious lever — trimming the tool surface this repo exposes — is worth about a
thousand tokens out of twenty-five thousand. The real weight is OpenClaw's own baseline:
thirteen plugins' worth of built-in tools, and the catalogue of all 58 discovered skills
injected so the agent knows what exists.

Applied together, the prompt on the wire went from **24,853 tokens to 8,193** — a 67%
cut, and reading it on the Orin dropped from 57 s to **5.7 s**. What moves it, in order:

1. `plugins.deny` for what this agent never touches — browser, canvas, cua-computer,
   file-transfer, geolocation, talk-voice, memory-core, xai. **Plugins loaded went from
   13 to 5.** It also silences the `[memory] sync failed: No API key found for provider
   "openai"` line that repeats through the gateway log.
2. `skills.limits.maxSkillsInPrompt` / `maxSkillsPromptChars`, so 57 skill descriptions
   the agent cannot use stop riding along.
3. `mcp.servers.johndeere.toolFilter.include` with five tools instead of fourteen. The
   smallest of the three, applied last.

**Two of these are for the local backend only.** The five-tool filter drops
`disable_machine` — the breakdown, which is the most visual moment of the demo — along
with `explain_last_decision` and the pause/restart controls. And `thinkingDefault: "off"`
exists to stop Qwen3 spending a minute reasoning; on Claude it just takes the reasoning
away. Both stay out of the default config and go in only when running on the Orin.
`plugins.deny` and `skills.limits` are safe either way and stay on.

**What does not work:** `agents.entries.*.tools.profile: "minimal"`. It looks like
exactly the right knob and it is a trap — it strips the MCP server from the agent
entirely, and `tools.alsoAllow: ["mcp__johndeere__*"]` does not bring it back. Asked to
read the fleet state under that setting the agent answered *"las herramientas MCP
johndeere no están cargadas en esta sesión"* and reached the endpoint over plain HTTP
instead. It got the right answer by the wrong road. The setting is left out.

### 11.9 The remote control: Telegram

The CLI and the Control UI both tie the presenter to a keyboard. With a chat channel
linked, the operator walks the stage, types *"se descompuso H2"* on a phone, and the field
reorganises on the screen behind them. It adds no information — it moves the controls.

Nothing in `johndeere/` or `Servidor/` changes. `farm-manager` is already the gateway's
default agent with its fourteen MCP tools; the channel is one more surface messages
arrive on.

**Telegram is the one to use**, and it needs no plugin — it ships inside OpenClaw
(`dist/telegram`). Create the bot from inside Telegram itself: message `@BotFather`,
send `/newbot`, give it a name and a username ending in `bot`, and it hands back a token.

```json5
channels: {
  telegram: {
    enabled: true,
    botToken: "<from BotFather>",
    dmPolicy: "pairing",
    groupPolicy: "disabled",
  },
},
bindings: [
  { type: "route", agentId: "farm-manager", match: { channel: "telegram" } },
],
```

`dmPolicy: "pairing"` rather than a raw allowlist because Telegram identifies senders by
numeric user id, which nobody knows by heart. The first message raises a request, one
`openclaw pairing approve telegram <CODE>` pins that sender for good, and everybody else
is dropped — which matters, because this agent can break machines and restart the
campaign. `groupPolicy: "disabled"` closes the other way in.

**Why not WhatsApp.** It was tried first and removed. Its channel links through Baileys —
WhatsApp Web automation, not the official business API — so it needs a real phone number,
a QR scan, and it puts the linked account at risk of a ban. A dedicated number means a
physical prepaid SIM; virtual numbers are mostly blocked by WhatsApp at registration. A
Telegram bot has no phone number at all, its token is revocable from `@BotFather` in
seconds, and the operator's personal account is never part of the setup.

**The field never texts first.** The watcher POSTs to `/hooks/agent` with no delivery
fields, so the turns it wakes run headless and nothing arrives unprompted. The channel
carries the operator's orders and the replies to them, and that is all. Turning that
around later is a config change, not a code one: `hooks.mappings` takes `channel`, `to`
and `deliver`.

`run-demo.sh` prints the channel's status in its banner, so a channel that came down
shows up before you are on stage rather than during.

### 11.10 What the local backend actually costs

Measured on the Orin, through the container, with the cut prompt:

| | Before the cuts | After |
|---|---|---|
| Prompt on the wire | 24,853 tokens | **8,193** |
| Reading it | 57 s (433 tok/s, 30B) | **5.7 s** (1,426 tok/s, 4B) |
| Generating | 15 tok/s | **29 tok/s** |

Reading the prompt stopped being the problem. What replaced it: Qwen3 reasons before
answering, and a single "how is the harvest going" produced **1,738 tokens** of thinking
at 29 tok/s — a minute of generation for one sentence of output. `thinkingDefault: "off"`
on the agent entry is the knob for it.

**Not yet working end to end.** An `openclaw agent --model orin/qwen3:4b` turn does not
reach the container: the gateway reports `LLM request failed: network connection error`
and Ollama logs no request, while a plain `curl` to the very same tunnelled endpoint
answers correctly. The tunnel, the container and the model are all verified good in
isolation, so the fault is in how OpenClaw reaches the provider, and it is unfinished.

Which does not change the recommendation. The Claude Pro backend is the one to demo:
verified end to end, ~15 s per decision, and it keeps up with a field being harvested in
front of an audience. The Orin is the comparison exhibit, and the numbers above are the
interesting part of it either way.

### 11.11 It runs unattended

Left alone for three minutes on a 16×22 field with 4 harvesters and 2 carts, no
operator touching anything:

```
tick 43  [harvester_waiting] H0 (12 ticks) has a full tank and no cart alongside
   t 47  get_fleet_state
   t 53  add_cart -> C2
   t 54  announce "H0 and H2 both full and stopped with both carts already busy"
   t 68  add_cart -> C3
tick 77  [work_lopsided] H1 has run out of work while H0 still has 56 cells
   t 81  get_fleet_state
   t 85  rebalance_zones -> 4 zones
   t 86  announce "H1 se quedó sin trabajo con 146 celdas en pie…"
```

Plans went from 56/0/51/53 to 30/33/32/35 and the idle ratio fell from 0.54 to
0.31. The supervisor read the state before every change, made one change per
wake-up, and said why — which is exactly what the skill asks for, and it is
`explain_last_decision` that produced the trace above.

### 11.12 Does it actually pay?

20 held-out seeds on a 16×22 field, comparing the plain engine against the same engine
rebalancing when a machine runs out of work while another still has a backlog of more
than 25 cells, at most once every 60 ticks. Both rows of every pair run the fixed engine,
so the deadlock fixes of §11.2 are not what is being measured here:

| Fleet | Ticks | Idle | L/unit |
|---|---|---|---|
| 2H/1C | 455 → 465 (+2%) | 589 → 577 (−2%) | 1.73 → 1.83 (+6%) |
| 3H/2C | 294 → 277 (−6%) | 587 → 495 (−16%) | 2.00 → 2.03 (+2%) |
| 4H/2C | 288 → 248 (**−14%**) | 809 → 581 (**−28%**) | 2.15 → 2.14 (−1%) |
| 5H/3C | 211 → 217 (+3%) | 767 → 729 (−5%) | 2.15 → 2.36 (+10%) |

Read honestly: it pays where the zones can actually go lopsided, and costs a little
where they cannot. With two harvesters there is not enough imbalance to be worth a drive;
with five on this field the logistics saturate before the cutting does, and calling a
parked machine back out spends fuel to save nothing. **Fuel generally goes up while time
goes down** — a machine that would have parked is now driving.

Which is the argument for the layer rather than against it. A fixed rule cannot tell
2H/1C from 4H/2C. The supervisor reads the fleet, the idle ratio and the spread before
deciding, and "do nothing" is an answer it is explicitly told to prefer.
