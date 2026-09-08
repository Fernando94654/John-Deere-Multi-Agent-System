"""What the supervisor is allowed to ask, and what it is allowed to change.

The split is deliberate: the reading tools describe the campaign in the terms a
decision is actually made in — what is left to cut, who is waiting on whom, how
much of the fleet is standing still — while the writing tools set goals and
never steer a machine. Nothing here plans a route or picks a cell to cut. That
stays in the engine, where the invariants live.

Each tool is a plain function. Its docstring is the description the model reads
to decide whether to reach for it, and its type hints are the input schema — so
the two things that must never drift apart are written once.

**Every tool is `async def`, and that is load-bearing.** The MCP SDK dispatches a
*sync* tool through `anyio.to_thread.run_sync`, which would run it on a worker
thread while the tick loop is midway through `Simulation.step()` on the event
loop — `add_cart` appending to `self.carts` inside `for cart in self.carts`, or
`prioritize` replacing a plan while `next_target` pops from it. A coroutine is
awaited on the loop instead, so a tool call and a tick can never interleave and
the engine's invariants hold without a single lock.
"""

from __future__ import annotations

import functools
from typing import Callable, Optional

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from .policy import Guard, as_float, as_int, machine_id

ROCK, CUT, FARM, UNOWNED = "#", ".", "F", "*"

#: A tool that only reports. Nothing it does needs the operator's approval.
READS = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
#: A tool that changes the world, and running it twice is the same as once.
CHANGES = ToolAnnotations(
    read_only_hint=False, idempotent_hint=True, open_world_hint=False
)
#: The same, but not idempotent — a second call adds a second cart.
ADDS = ToolAnnotations(
    read_only_hint=False, idempotent_hint=False, open_world_hint=False
)
#: A tool that takes something away that cannot simply be put back.
BREAKS = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True,
    open_world_hint=False,
)


def field_view(sim) -> list[str]:
    """The field as text: who owns the crop that is still standing.

    A digit is standing crop owned by that harvester, `*` standing crop nobody
    owns yet, `.` ground already cut, `#` a rock and `F` the farm. One picture
    that answers both "how much is left" and "whose is it", which is what a
    decision about regions and rebalancing needs.
    """
    owners = sim.zone_map()
    rows = []
    for row in range(sim.field.rows):
        line = []
        for col in range(sim.field.cols):
            cell = (row, col)
            if cell == sim.farm:
                line.append(FARM)
            elif not sim.field.passable(cell):
                line.append(ROCK)
            elif not sim.field.has_food(cell):
                line.append(CUT)
            else:
                owner = owners[row * sim.field.cols + col]
                line.append(UNOWNED if owner < 0 else str(owner % 10))
        rows.append("".join(line))
    return rows


def build_server(session, guard: Guard) -> MCPServer:
    """Assemble the tool surface for one session, each call behind the guard."""
    mcp = MCPServer("johndeere-harvest", version="1.0.0")

    def sim():
        """The live run, or a refusal the model can act on."""
        if session.sim is None:
            raise ToolError(
                "no run is in progress; ask the operator to start one, or call "
                "restart_run to build one"
            )
        return session.sim

    def guarded(mutating: bool = False) -> Callable:
        """Put one tool behind the budget, the argument checks and the log.

        This is the only way into the world, so it wraps *every* tool rather
        than only the ones that change something: a refused read still belongs
        in the audit trail the operator reads back.
        """

        def decorate(func: Callable) -> Callable:
            @functools.wraps(func)
            async def run(*args, **kwargs):
                tick = session.sim.tick if session.sim is not None else 0
                arguments = dict(kwargs)

                refusal = guard.refusal(func.__name__, mutating, tick)
                if refusal is not None:
                    guard.record(tick, func.__name__, arguments, False, refusal)
                    raise ToolError(refusal)

                try:
                    outcome = await func(*args, **kwargs)
                except (ValueError, ToolError) as error:
                    # An outcome the model should read and correct, not a transport failure.
                    guard.record(tick, func.__name__, arguments, False, str(error))
                    raise ToolError(str(error)) from None

                if mutating:
                    guard.charge(tick)
                guard.record(
                    tick, func.__name__, arguments, True, _summary(func.__name__, outcome)
                )
                return outcome

            return run

        return decorate

    # --- reading ---------------------------------------------------------
    @mcp.tool(annotations=READS)
    @guarded()
    async def get_fleet_state() -> dict:
        """How the campaign stands: crop left, every machine's state, load,
        remaining work and waiting time, open unload requests, the rolling idle
        ratio and the policy in force. Read this before changing anything."""
        state = sim().diagnostics()
        state["status"] = session.status
        state["run_id"] = session.run_id
        return state

    @mcp.tool(annotations=READS)
    @guarded()
    async def get_field_map() -> dict:
        """The field as a picture: which harvester owns each cell of standing
        crop, what is already cut, where the rocks and the farm are. Use it to
        pick the corners of a region."""
        current = sim()
        return {
            "legend": {
                "digit": "standing crop owned by that harvester",
                UNOWNED: "standing crop with no owner",
                CUT: "already cut",
                ROCK: "rock",
                FARM: "the farm",
            },
            "rows": current.field.rows,
            "columns": current.field.cols,
            "map": field_view(current),
        }

    @mcp.tool(annotations=READS)
    @guarded()
    async def explain_last_decision(limit: int = 10) -> dict:
        """The supervisor's own recent commands and what became of them,
        refusals included. This is what the operator is shown when they ask why
        the fleet did something."""
        return {"decisions": guard.log(as_int({"limit": limit}, "limit", 1, 50))}

    @mcp.tool(annotations=READS)
    @guarded()
    async def list_recent_events(limit: int = 10) -> dict:
        """The events the simulation raised: a machine idle too long, the work
        gone lopsided, a breakdown. These are what woke you."""
        return {"events": session.watcher.recent(as_int({"limit": limit}, "limit", 1, 50))}

    # --- writing ---------------------------------------------------------
    @mcp.tool(annotations=CHANGES)
    @guarded(mutating=True)
    async def rebalance_zones() -> dict:
        """Redraw the work zones over the crop that is still standing, seeded
        where the machines are now. Hands leftover work to whoever can reach it,
        and calls a harvester that had already parked back out. Costs fuel in
        driving, so it pays when the work is lopsided and not otherwise."""
        return sim().rebalance()

    @mcp.tool(annotations=BREAKS)
    @guarded(mutating=True)
    async def disable_machine(harvester: str | int) -> dict:
        """Break a harvester down where it stands: it stops working and becomes
        an obstacle, its cart is released, the grain in its tank is stranded and
        its zone is shared out. This is the breakdown drill, not a repair.

        Args:
            harvester: the machine, as 'H1' or 1.
        """
        current = sim()
        target = machine_id({"harvester": harvester}, "harvester", len(current.harvesters))
        if sum(not h.disabled for h in current.harvesters) <= 1:
            raise ToolError(
                "that is the last harvester still running; breaking it down "
                "would leave nobody to cut the field"
            )
        changed = current.disable(target)
        return {
            "harvester": f"H{target}",
            "disabled": changed,
            "note": "already broken down" if not changed else "zone handed to the others",
            "zones": [
                {"harvester": h.label, "crop": len(h.plan)}
                for h in current.harvesters
                if not h.disabled
            ],
        }

    @mcp.tool(annotations=CHANGES)
    @guarded(mutating=True)
    async def repair_machine(harvester: str | int) -> dict:
        """Put a broken harvester back to work and give it a share of what is left.

        Args:
            harvester: the machine, as 'H1' or 1.
        """
        current = sim()
        target = machine_id({"harvester": harvester}, "harvester", len(current.harvesters))
        return {"harvester": f"H{target}", "repaired": current.repair(target)}

    @mcp.tool(annotations=CHANGES)
    @guarded(mutating=True)
    async def prioritize_region(
        top_row: int, left_column: int, bottom_row: int, right_column: int
    ) -> dict:
        """Send the whole fleet at one rectangle of the field first — the strip
        rain is coming to, or the corner that has to be clear for a delivery.
        Rows grow downward, columns rightward, both ends included."""
        current = sim()
        bounds = {
            "top_row": top_row, "left_column": left_column,
            "bottom_row": bottom_row, "right_column": right_column,
        }
        # Not in the schema: the limits are this field's size and change every run.
        top = as_int(bounds, "top_row", 0, current.field.rows - 1)
        left = as_int(bounds, "left_column", 0, current.field.cols - 1)
        bottom = as_int(bounds, "bottom_row", 0, current.field.rows - 1)
        right = as_int(bounds, "right_column", 0, current.field.cols - 1)
        promoted = current.prioritize((top, left), (bottom, right))
        return {
            "region": {"top": top, "left": left, "bottom": bottom, "right": right},
            "cells_promoted": promoted,
            "note": (
                "no standing crop inside that region"
                if not promoted
                else "the fleet works this region first"
            ),
        }

    @mcp.tool(annotations=CHANGES)
    @guarded(mutating=True)
    async def set_policy(
        request_threshold: Optional[float] = None,
        wait_weight: Optional[float] = None,
    ) -> dict:
        """Retune the coordination. request_threshold is the share of its tank at
        which a harvester calls for a cart: lower it when machines are waiting,
        raise it when carts are running half empty. wait_weight is how much a
        long wait discounts a cart's bid: raise it when one harvester keeps
        being passed over."""
        given = {"request_threshold": request_threshold, "wait_weight": wait_weight}
        threshold = as_float(given, "request_threshold", 0.05, 1.0)
        weight = as_float(given, "wait_weight", 0.0, 10.0)
        if threshold is None and weight is None:
            raise ToolError("give at least one of request_threshold or wait_weight")
        return sim().set_policy(request_threshold=threshold, wait_weight=weight)

    @mcp.tool(annotations=ADDS)
    @guarded(mutating=True)
    async def add_cart() -> dict:
        """Send one more grain cart out from the farm. The right answer when the
        harvesters are idle waiting to be emptied rather than idle for room."""
        current = sim()
        if len(current.carts) >= 6:
            raise ToolError("six carts is the most this field can hold without gridlock")
        return {"cart": f"C{current.add_cart()}", "fleet_carts": len(current.carts)}

    @mcp.tool(annotations=CHANGES)
    @guarded()
    async def announce(text: str) -> dict:
        """Caption the field with one short line saying what you are doing and
        why, in the operator's language. Call it alongside a change so the people
        watching the simulation can follow the reasoning."""
        text = text.strip()
        if not text:
            raise ToolError("say something: 'text' is what gets shown on screen")
        if len(text) > 160:
            raise ToolError(
                f"keep it to 160 characters, that was {len(text)} — it is a caption "
                "under a field, not a paragraph"
            )
        session.narration = {
            "text": text,
            "tick": session.sim.tick if session.sim is not None else 0,
        }
        return {"shown": text}

    @mcp.tool(annotations=CHANGES)
    @guarded()
    async def pause_run() -> dict:
        """Stop advancing ticks; the world keeps its state."""
        session.pause()
        return {"status": session.status}

    @mcp.tool(annotations=CHANGES)
    @guarded()
    async def resume_run() -> dict:
        """Carry on from the tick where the run stopped."""
        session.resume()
        return {"status": session.status}

    @mcp.tool(annotations=BREAKS)
    @guarded(mutating=True)
    async def restart_run(
        rows: Optional[int] = None,
        columns: Optional[int] = None,
        new_seed: bool = False,
    ) -> dict:
        """Rebuild the campaign from tick 1. The field is identical unless
        new_seed is set, so a demo can be rehearsed and repeated."""
        session.restart(
            int(rows) if rows is not None else session.rows,
            int(columns) if columns is not None else session.cols,
            new_seed=bool(new_seed),
        )
        return {"restarting": True, "rows": session.rows, "columns": session.cols}

    return mcp


def _summary(name: str, outcome) -> str:
    """One line for the log: enough to read the history without the full payload."""
    if not isinstance(outcome, dict):
        return name
    for key in ("shown", "note", "cells_promoted", "rebalanced", "cart", "status", "restarting"):
        if key in outcome:
            return f"{name}: {key}={outcome[key]}"
    return name
