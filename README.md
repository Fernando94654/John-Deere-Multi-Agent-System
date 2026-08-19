# John-Deere-Multi-Agent-System

A multi-agent simulation of a harvest campaign: several **harvesters** sweep a field and
hand their grain to several mobile **grain carts**, which ferry it to the **farm**. Pure
Python, with no dependency beyond `matplotlib` for the 2D view.

```bash
python3 main.py --frontend visual --harvesters 3 --carts 3 --seed 42 --interval 125
python3 main.py --harvesters 3 --carts 2 --seed 42
python3 main.py --frontend visual --harvesters 3 --carts 2 --save run.gif
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
```

Dependencies run one way only:

```
world  ->  planning  ->  agents  ->  coordination  ->  simulation  ->  frontends
```

**The engine never prints or draws.** Frontends observe a run through an immutable
`Snapshot` per tick, which is the only contract between the two sides.

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
                │  ▲            │
                ▼  │            ▼
            rotating ──────► unloading
```

| State | What it is doing |
|---|---|
| `to zone` | Driving from the farm to its zone |
| `harvesting` | Sweeping its zone; cuts the cell it stands on |
| `waiting cart` | Tank full (or zone finished with grain aboard) and no cart alongside |
| `rotating` | Turning 90° to bring the spout round to the cart |
| `unloading` | Stopped, cart on its left, passing grain across |
| `returning` | Zone finished and tank empty: driving home |
| `done` | Parked at the farm, day over |

It calls for a cart at the threshold and **keeps cutting** while it waits. It only stops if
it fills up with no cart docked, or if it finishes its zone with grain aboard.

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

**End of campaign**: no reachable crop left, every harvester back at the farm, and every
cart empty and parked. `max_ticks` is the safety net.

---

## 6. Metrics (`metrics.py`)

Per machine: distance, fuel, idle ticks. Per fleet: harvested, delivered, in transit, CO₂
(`litres × 2.68`), litres per unit delivered, and right-of-way stops.

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
- **Every** transfer happens with the cart on the left-hand side.
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
