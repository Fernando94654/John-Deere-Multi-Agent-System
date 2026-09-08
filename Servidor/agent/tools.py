"""What the supervisor is allowed to ask, and what it is allowed to change.

The split is deliberate: the reading tools describe the campaign in the terms a
decision is actually made in — what is left to cut, who is waiting on whom, how
much of the fleet is standing still — while the writing tools set goals and
never steer a machine. Nothing here plans a route or picks a cell to cut. That
stays in the engine, where the invariants live.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from .mcp import Tool
from .policy import Guard, as_float, as_int, machine_id

NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}

ROCK, CUT, FARM, UNOWNED = "#", ".", "F", "*"


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


def build_tools(session, guard: Guard) -> list[Tool]:
    """Assemble the tool surface for one session, each call behind the guard."""

    def sim():
        """The live run, or a refusal the model can act on."""
        if session.sim is None:
            raise ValueError(
                "no run is in progress; ask the operator to start one, or call "
                "restart_run to build one"
            )
        return session.sim

    # --- reading ---------------------------------------------------------
    def get_fleet_state(_: dict) -> dict:
        state = sim().diagnostics()
        state["status"] = session.status
        state["run_id"] = session.run_id
        return state

    def get_field_map(_: dict) -> dict:
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

    def explain_last_decision(arguments: dict) -> dict:
        limit = as_int({"limit": arguments.get("limit", 10)}, "limit", 1, 50)
        return {"decisions": guard.log(limit)}

    def list_recent_events(arguments: dict) -> dict:
        limit = as_int({"limit": arguments.get("limit", 10)}, "limit", 1, 50)
        return {"events": session.watcher.recent(limit)}

    # --- writing ---------------------------------------------------------
    def rebalance_zones(_: dict) -> dict:
        return sim().rebalance()

    def disable_machine(arguments: dict) -> dict:
        current = sim()
        target = machine_id(arguments, "harvester", len(current.harvesters))
        if sum(not h.disabled for h in current.harvesters) <= 1:
            raise ValueError(
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

    def repair_machine(arguments: dict) -> dict:
        current = sim()
        target = machine_id(arguments, "harvester", len(current.harvesters))
        return {"harvester": f"H{target}", "repaired": current.repair(target)}

    def prioritize_region(arguments: dict) -> dict:
        current = sim()
        top = as_int(arguments, "top_row", 0, current.field.rows - 1)
        left = as_int(arguments, "left_column", 0, current.field.cols - 1)
        bottom = as_int(arguments, "bottom_row", 0, current.field.rows - 1)
        right = as_int(arguments, "right_column", 0, current.field.cols - 1)
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

    def set_policy(arguments: dict) -> dict:
        threshold = as_float(arguments, "request_threshold", 0.05, 1.0)
        weight = as_float(arguments, "wait_weight", 0.0, 10.0)
        if threshold is None and weight is None:
            raise ValueError("give at least one of request_threshold or wait_weight")
        return sim().set_policy(request_threshold=threshold, wait_weight=weight)

    def add_cart(_: dict) -> dict:
        current = sim()
        if len(current.carts) >= 6:
            raise ValueError("six carts is the most this field can hold without gridlock")
        return {"cart": f"C{current.add_cart()}", "fleet_carts": len(current.carts)}

    def announce(arguments: dict) -> dict:
        text = str(arguments.get("text", "")).strip()
        if not text:
            raise ValueError("say something: 'text' is what gets shown on screen")
        if len(text) > 160:
            raise ValueError(
                f"keep it to 160 characters, that was {len(text)} — it is a caption "
                "under a field, not a paragraph"
            )
        session.narration = {
            "text": text,
            "tick": session.sim.tick if session.sim is not None else 0,
        }
        return {"shown": text}

    def pause_run(_: dict) -> dict:
        session.pause()
        return {"status": session.status}

    def resume_run(_: dict) -> dict:
        session.resume()
        return {"status": session.status}

    def restart_run(arguments: dict) -> dict:
        rows = arguments.get("rows")
        columns = arguments.get("columns")
        session.restart(
            int(rows) if rows is not None else session.rows,
            int(columns) if columns is not None else session.cols,
            new_seed=bool(arguments.get("new_seed")),
        )
        return {"restarting": True, "rows": session.rows, "columns": session.cols}

    limit_schema = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}
        },
        "additionalProperties": False,
    }
    harvester_schema = {
        "type": "object",
        "properties": {
            "harvester": {
                "type": ["string", "integer"],
                "description": "the machine, as 'H1' or 1",
            }
        },
        "required": ["harvester"],
        "additionalProperties": False,
    }

    raw = [
        Tool(
            "get_fleet_state",
            "How the campaign stands: crop left, every machine's state, load, "
            "remaining work and waiting time, open unload requests, the rolling "
            "idle ratio and the policy in force. Read this before changing anything.",
            NO_ARGS,
            get_fleet_state,
        ),
        Tool(
            "get_field_map",
            "The field as a picture: which harvester owns each cell of standing "
            "crop, what is already cut, where the rocks and the farm are. Use it "
            "to pick the corners of a region.",
            NO_ARGS,
            get_field_map,
        ),
        Tool(
            "explain_last_decision",
            "The supervisor's own recent commands and what became of them, "
            "refusals included. This is what the operator is shown when they ask "
            "why the fleet did something.",
            limit_schema,
            explain_last_decision,
        ),
        Tool(
            "list_recent_events",
            "The events the simulation raised: a machine idle too long, the work "
            "gone lopsided, a breakdown. These are what woke you.",
            limit_schema,
            list_recent_events,
        ),
        Tool(
            "rebalance_zones",
            "Redraw the work zones over the crop that is still standing, seeded "
            "where the machines are now. Hands leftover work to whoever can reach "
            "it, and calls a harvester that had already parked back out. Costs "
            "fuel in driving, so it pays when the work is lopsided and not otherwise.",
            NO_ARGS,
            rebalance_zones,
            mutating=True,
            idempotent=True,
        ),
        Tool(
            "disable_machine",
            "Break a harvester down where it stands: it stops working and becomes "
            "an obstacle, its cart is released, the grain in its tank is stranded "
            "and its zone is shared out. This is the breakdown drill, not a repair.",
            harvester_schema,
            disable_machine,
            mutating=True,
            destructive=True,
        ),
        Tool(
            "repair_machine",
            "Put a broken harvester back to work and give it a share of what is left.",
            harvester_schema,
            repair_machine,
            mutating=True,
        ),
        Tool(
            "prioritize_region",
            "Send the whole fleet at one rectangle of the field first — the strip "
            "rain is coming to, or the corner that has to be clear for a delivery. "
            "Rows grow downward, columns rightward, both ends included.",
            {
                "type": "object",
                "properties": {
                    "top_row": {"type": "integer", "minimum": 0},
                    "left_column": {"type": "integer", "minimum": 0},
                    "bottom_row": {"type": "integer", "minimum": 0},
                    "right_column": {"type": "integer", "minimum": 0},
                },
                "required": ["top_row", "left_column", "bottom_row", "right_column"],
                "additionalProperties": False,
            },
            prioritize_region,
            mutating=True,
            idempotent=True,
        ),
        Tool(
            "set_policy",
            "Retune the coordination. request_threshold is the share of its tank "
            "at which a harvester calls for a cart: lower it when machines are "
            "waiting, raise it when carts are running half empty. wait_weight is "
            "how much a long wait discounts a cart's bid: raise it when one "
            "harvester keeps being passed over.",
            {
                "type": "object",
                "properties": {
                    "request_threshold": {
                        "type": "number", "minimum": 0.05, "maximum": 1.0
                    },
                    "wait_weight": {"type": "number", "minimum": 0.0, "maximum": 10.0},
                },
                "additionalProperties": False,
            },
            set_policy,
            mutating=True,
            idempotent=True,
        ),
        Tool(
            "add_cart",
            "Send one more grain cart out from the farm. The right answer when the "
            "harvesters are idle waiting to be emptied rather than idle for room.",
            NO_ARGS,
            add_cart,
            mutating=True,
        ),
        Tool(
            "announce",
            "Caption the field with one short line saying what you are doing and "
            "why, in the operator's language. Call it alongside a change so the "
            "people watching the simulation can follow the reasoning.",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": 160},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            announce,
        ),
        Tool("pause_run", "Stop advancing ticks; the world keeps its state.", NO_ARGS, pause_run),
        Tool("resume_run", "Carry on from the tick where the run stopped.", NO_ARGS, resume_run),
        Tool(
            "restart_run",
            "Rebuild the campaign from tick 1. The field is identical unless "
            "new_seed is set, so a demo can be rehearsed and repeated.",
            {
                "type": "object",
                "properties": {
                    "rows": {"type": "integer", "minimum": 6, "maximum": 60},
                    "columns": {"type": "integer", "minimum": 6, "maximum": 60},
                    "new_seed": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
            restart_run,
            mutating=True,
            destructive=True,
        ),
    ]

    return [_guarded(tool, session, guard) for tool in raw]


def _guarded(tool: Tool, session, guard: Guard) -> Tool:
    """Wrap one tool so the budget, the checks and the log all apply to it."""

    def run(arguments: dict):
        tick = session.sim.tick if session.sim is not None else 0
        refusal: Optional[str] = guard.refusal(tool.name, tool.mutating, tick)
        if refusal is not None:
            guard.record(tick, tool.name, arguments, False, refusal)
            raise ValueError(refusal)

        try:
            outcome = tool.run(arguments)
        except ValueError as error:
            guard.record(tick, tool.name, arguments, False, str(error))
            raise

        if tool.mutating:
            guard.charge(tick)
        guard.record(tick, tool.name, arguments, True, _summary(tool.name, outcome))
        return outcome

    return replace(tool, run=run)


def _summary(name: str, outcome) -> str:
    """One line for the log: enough to read the history without the full payload."""
    if not isinstance(outcome, dict):
        return name
    for key in ("shown", "note", "cells_promoted", "rebalanced", "cart", "status", "restarting"):
        if key in outcome:
            return f"{name}: {key}={outcome[key]}"
    return name
