"""The supervision layer: an MCP tool surface over a running simulation.

`server.py` streams the world to Unity. This package lets an agent framework —
OpenClaw, or anything else that speaks MCP — watch the same run and steer it at
the level of goals: rebalance the zones, work this region first, that machine
has broken down. It never drives a machine.

    tools.py     what the supervisor may ask and may change
    policy.py    the gate every command passes through, and the audit trail
    watcher.py   what wakes the agent when the fleet gets into trouble

The protocol itself is the official MCP SDK's; only the tools are ours.
"""

from .policy import Guard
from .tools import build_server
from .watcher import Watcher

__all__ = ["Guard", "build_server", "Watcher"]
