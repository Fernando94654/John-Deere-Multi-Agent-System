"""The gate every supervisor command passes through before it reaches the world.

NemoClaw pairs its agent with OpenShell, a runtime that decides what the model
is actually permitted to execute. That runtime is in preview, so the same idea
is implemented here, in the one place it can be audited: an allowlist, a budget,
argument checks, and a log of every call — including the refused ones.

The point is not that the model is untrustworthy. It is that the guarantees in
§8 of the README (no collisions, no cart on standing crop, the campaign always
closes) are properties of the engine, and they stay properties of the engine
only if nothing outside it can reach past this gate.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Optional

#: World-changing calls allowed per window: steering the fleet, not shaking it.
DEFAULT_BUDGET = 3
DEFAULT_WINDOW = 60
#: The same window in seconds, so a paused run still refills the budget.
DEFAULT_SECONDS = 30.0


@dataclass
class Decision:
    """One command the supervisor tried to run, and what became of it."""

    tick: int
    tool: str
    arguments: dict
    allowed: bool
    detail: str

    def as_json(self) -> dict:
        return {
            "tick": self.tick,
            "tool": self.tool,
            "arguments": self.arguments,
            "outcome": "applied" if self.allowed else "refused",
            "detail": self.detail,
        }


@dataclass
class Guard:
    """Allowlist, budget and audit trail for the supervisor's commands."""

    budget: int = DEFAULT_BUDGET
    window: int = DEFAULT_WINDOW
    seconds: float = DEFAULT_SECONDS
    decisions: deque[Decision] = dataclass_field(
        default_factory=lambda: deque(maxlen=200)
    )
    #: (tick, wall clock) of each change made, oldest first.
    _spent: deque[tuple[int, float]] = dataclass_field(default_factory=deque)

    def _expire(self, tick: int) -> None:
        """Drop the changes that have aged out of either window."""
        now = time.monotonic()
        while self._spent and (
            tick - self._spent[0][0] >= self.window
            or now - self._spent[0][1] >= self.seconds
        ):
            self._spent.popleft()

    def refusal(self, tool_name: str, mutating: bool, tick: int) -> Optional[str]:
        """Why this call may not run, or `None` when it may."""
        if not mutating:
            return None
        self._expire(tick)
        if len(self._spent) >= self.budget:
            waited = time.monotonic() - self._spent[0][1]
            return (
                f"budget spent: {self.budget} changes per {self.window} ticks "
                f"or {self.seconds:.0f}s. The oldest was {waited:.0f}s ago. Read "
                f"the state and wait — the fleet needs time to act on the last "
                f"order before the next one is worth giving."
            )
        return None

    def charge(self, tick: int) -> None:
        """Book one world-changing call against the budget."""
        self._spent.append((tick, time.monotonic()))

    def record(
        self, tick: int, tool: str, arguments: dict, allowed: bool, detail: str
    ) -> None:
        self.decisions.append(Decision(tick, tool, arguments, allowed, detail))

    def log(self, limit: int = 10) -> list[dict]:
        """The most recent commands, newest last."""
        return [d.as_json() for d in list(self.decisions)[-limit:]]


def as_int(arguments: dict, name: str, low: int, high: int) -> int:
    """Read a bounded integer argument, or say plainly what was wrong with it."""
    if name not in arguments:
        return _fail(f"missing required argument {name!r}")
    try:
        value = int(arguments[name])
    except (TypeError, ValueError):
        return _fail(f"{name!r} must be a whole number, got {arguments[name]!r}")
    if not low <= value <= high:
        return _fail(f"{name!r} must lie between {low} and {high}, got {value}")
    return value


def as_float(arguments: dict, name: str, low: float, high: float) -> Optional[float]:
    """Read an optional bounded number; `None` when the argument was omitted."""
    if arguments.get(name) is None:
        return None
    try:
        value = float(arguments[name])
    except (TypeError, ValueError):
        return _fail(f"{name!r} must be a number, got {arguments[name]!r}")
    if not low <= value <= high:
        return _fail(f"{name!r} must lie between {low} and {high}, got {value}")
    return value


def machine_id(arguments: dict, name: str, count: int) -> int:
    """Read a harvester as either `2` or `"H2"`, and check it exists."""
    raw: Any = arguments.get(name)
    if raw is None:
        return _fail(f"missing required argument {name!r}")
    if isinstance(raw, str):
        raw = raw.strip().upper().removeprefix("H")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _fail(f"{name!r} should be a harvester like 'H1' or 1, got {arguments[name]!r}")
    if not 0 <= value < count:
        return _fail(f"there is no harvester {value}: the fleet runs H0 to H{count - 1}")
    return value


def _fail(message: str):
    raise ValueError(message)
