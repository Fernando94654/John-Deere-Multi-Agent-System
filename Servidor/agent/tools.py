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

from .policy import Guard
from Servidor.controls import RunControls

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
    controls = RunControls(session)

    def sim():
        """The live run, or a refusal the model can act on."""
        return controls.current()

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
        return controls.rebalance()

    @mcp.tool(annotations=BREAKS)
    @guarded(mutating=True)
    async def disable_machine(harvester: str | int) -> dict:
        """Break a harvester down where it stands: it stops working and becomes
        an obstacle, its cart is released, the grain in its tank is stranded and
        its zone is shared out. This is the breakdown drill, not a repair.

        Args:
            harvester: the machine, as 'H1' or 1.
        """
        result = controls.machine('disable', harvester)
        return {**result, 'disabled': result['changed']}

    @mcp.tool(annotations=CHANGES)
    @guarded(mutating=True)
    async def repair_machine(harvester: str | int) -> dict:
        """Put a broken harvester back to work and give it a share of what is left.

        Args:
            harvester: the machine, as 'H1' or 1.
        """
        result = controls.machine('repair', harvester)
        return {**result, 'repaired': result['changed']}

    @mcp.tool(annotations=CHANGES)
    @guarded(mutating=True)
    async def prioritize_region(
        top_row: int, left_column: int, bottom_row: int, right_column: int
    ) -> dict:
        """Send the whole fleet at one rectangle of the field first — the strip
        rain is coming to, or the corner that has to be clear for a delivery.
        Rows grow downward, columns rightward, both ends included."""
        return controls.prioritize(top_row, left_column, bottom_row, right_column)

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
        return controls.policy(request_threshold, wait_weight)

    @mcp.tool(annotations=ADDS)
    @guarded(mutating=True)
    async def add_cart() -> dict:
        """Send one more grain cart out from the farm. The right answer when the
        harvesters are idle waiting to be emptied rather than idle for room."""
        return controls.add_cart()

    @mcp.tool(annotations=CHANGES)
    @guarded()
    async def announce(text: str) -> dict:
        """Caption the field with one short line saying what you are doing and
        why, in the operator's language. Call it alongside a change so the people
        watching the simulation can follow the reasoning."""
        return controls.announce(text)

    @mcp.tool(annotations=CHANGES)
    @guarded()
    async def pause_run() -> dict:
        """Stop advancing ticks; the world keeps its state."""
        return controls.command("pause")

    @mcp.tool(annotations=CHANGES)
    @guarded()
    async def resume_run() -> dict:
        """Carry on from the tick where the run stopped."""
        return controls.command("resume")

    @mcp.tool(annotations=BREAKS)
    @guarded(mutating=True)
    async def restart_run(
        rows: Optional[int] = None,
        columns: Optional[int] = None,
        new_seed: bool = False,
        harvesters: Optional[int] = None,
        carts: Optional[int] = None,
        min_obstacles: Optional[int] = None,
        max_obstacles: Optional[int] = None,
        priority_region: Optional[str] = None,
    ) -> dict:
        """Restart with optional field dimensions, harvester and grain-cart counts.
        Omitted values keep current settings (initially server defaults). Use
        harvesters=2, carts=2 for two of each; never simulate breakdowns to resize
        a fleet. The new field exists on return, so prioritize_region can follow.
        For restart plus north priority, pass priority_region="north" in this call;
        also accepts south/east/west. No reset/start sequence is needed.
        Only restart when the operator requests it. Same seed unless new_seed.
        """
        result = controls.command('restart', dict(rows=rows, columns=columns,
            new_seed=new_seed, harvesters=harvesters, carts=carts,
            min_obstacles=min_obstacles, max_obstacles=max_obstacles, priority_region=priority_region))
        return {**result, 'restarting': False, 'restarted': True, **result['applied'],
                'columns': session.cols}

    @mcp.tool(annotations=READS)
    @guarded()
    async def get_run_config() -> dict:
        """Read current/default dimensions and fleet counts, even before starting."""
        return {**session.parameters(), 'status': session.status, 'ready': session.sim is not None}

    @mcp.tool(annotations=CHANGES)
    @guarded()
    async def start_run(rows: Optional[int] = None, columns: Optional[int] = None,
                        harvesters: Optional[int] = None, carts: Optional[int] = None) -> dict:
        """Start the first campaign with optional settings, or resume the existing one.
        Connecting to the server does not start a campaign. Use restart_run to resize.
        """
        return controls.command('start', dict(rows=rows, columns=columns,
                                             harvesters=harvesters, carts=carts))

    @mcp.tool(annotations=BREAKS)
    @guarded(mutating=True)
    async def reset_run() -> dict:
        """Rebuild the same field and fleet, paused at the opening frame."""
        return controls.command('reset')

    return mcp


def _summary(name: str, outcome) -> str:
    """One line for the log: enough to read the history without the full payload."""
    if not isinstance(outcome, dict):
        return name
    for key in ("shown", "note", "cells_promoted", "rebalanced", "cart", "status", "restarting"):
        if key in outcome:
            return f"{name}: {key}={outcome[key]}"
    return name
