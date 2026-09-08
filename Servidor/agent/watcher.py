"""What wakes the supervisor.

OpenClaw's heartbeat runs on a clock measured in minutes. A campaign runs at
half a second per tick, so a scheduled heartbeat would either sleep through the
whole harvest or wake up with nothing to say. The gateway also takes wakes on
demand, and that is the right shape here: the simulation knows the moment
something goes wrong, and says so.

Each condition is cheap to evaluate, debounced so a situation that lasts two
hundred ticks raises one event rather than two hundred, and phrased as a fact
about the world rather than an instruction. Deciding what to do about it is the
agent's job, and refusing to do anything is a valid answer.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Optional

#: A situation has to persist this long before it is worth waking anybody.
WAITING_TICKS = 12
#: And the same situation stays quiet for this long afterwards.
DEBOUNCE = 80
#: Work is lopsided when somebody is out of work and somebody else has this much.
BACKLOG = 12
#: Sustained share of the fleet standing still that counts as a bottleneck.
IDLE_RATIO = 0.45


async def post_json(url: str, payload: dict, token: Optional[str] = None) -> int:
    """POST `payload` as JSON and return the status code; 0 if it never landed.

    Blocking `urllib` on a worker thread: the supervisor is woken a handful of
    times per campaign, so a thread hop costs nothing and saves a dependency.
    This is the one piece of HTTP the simulation speaks *outwards*, which is why
    it lives here and not with the MCP server the SDK provides.
    """

    def send() -> int:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except OSError:
            return 0

    return await asyncio.to_thread(send)


@dataclass
class Event:
    """Something the fleet should probably be told about."""

    tick: int
    kind: str
    detail: str

    def as_json(self) -> dict:
        return {"tick": self.tick, "kind": self.kind, "detail": self.detail}


class Watcher:
    """Spots trouble after each tick and wakes the agent at most once per issue."""

    def __init__(
        self,
        wake_url: Optional[str] = None,
        token: Optional[str] = None,
        agent_id: Optional[str] = None,
        session_key: Optional[str] = None,
        debounce: int = DEBOUNCE,
    ):
        self.wake_url = wake_url
        self.token = token
        self.agent_id = agent_id
        self.session_key = session_key
        self.debounce = debounce
        self.events: deque[Event] = deque(maxlen=100)
        self.delivered = 0
        self.failed = 0
        self._last: dict[str, int] = {}

    def recent(self, limit: int = 10) -> list[dict]:
        """The latest events, newest last."""
        return [event.as_json() for event in list(self.events)[-limit:]]

    def scan(self, sim) -> list[Event]:
        """Every condition worth raising this tick, already debounced."""
        found: list[Event] = []
        for kind, detail in self._conditions(sim):
            previous = self._last.get(kind)
            if previous is not None and sim.tick - previous < self.debounce:
                continue
            self._last[kind] = sim.tick
            event = Event(sim.tick, kind, detail)
            self.events.append(event)
            found.append(event)
        return found

    def _conditions(self, sim):
        """The predicates themselves, each a fact and its plain-language shape."""
        stalled = [
            h for h in sim.harvesters if h.waiting_ticks >= WAITING_TICKS
        ]
        if stalled:
            who = ", ".join(
                f"{h.label} ({h.waiting_ticks} ticks)" for h in stalled
            )
            yield "harvester_waiting", (
                f"{who} has a full tank and no cart alongside, so it has stopped "
                f"cutting. There are {len(sim.carts)} carts on the field."
            )

        active = [h for h in sim.harvesters if not h.disabled]
        spare = [h for h in active if not h.plan]
        backlog = max((len(h.plan) for h in active), default=0)
        if spare and backlog >= BACKLOG:
            busiest = max(active, key=lambda h: len(h.plan))
            yield "work_lopsided", (
                f"{', '.join(h.label for h in spare)} has run out of work while "
                f"{busiest.label} still has {backlog} cells to cut."
            )

        if sim.idle_ratio >= IDLE_RATIO and sim.tick > 40:
            yield "fleet_idle", (
                f"{sim.idle_ratio:.0%} of the fleet has been standing still over "
                f"the last stretch, with {sim.food_left_reachable()} cells left."
            )

        broken = [h for h in sim.harvesters if h.disabled]
        if broken:
            yield "machine_down", (
                f"{', '.join(h.label for h in broken)} is broken down and out of "
                f"the campaign."
            )

    async def wake(self, events: list[Event], sim) -> None:
        """Tell the gateway. A gateway that is not there changes nothing."""
        if not self.wake_url or not events:
            return
        message = (
            "The harvest simulation raised: "
            + "; ".join(f"[{e.kind}] {e.detail}" for e in events)
            + f" Tick {sim.tick}. Read get_fleet_state before deciding, and say "
            "plainly if the right call is to do nothing."
        )
        # /hooks/agent reads `message`, /hooks/wake reads `text`; send both.
        payload: dict = {"message": message, "text": message}
        if self.agent_id:
            payload["agentId"] = self.agent_id
        if self.session_key:
            payload["sessionKey"] = self.session_key
        status = await post_json(self.wake_url, payload, self.token)
        if 200 <= status < 300:
            self.delivered += 1
        else:
            self.failed += 1
