"""The supervision layer: an MCP tool surface over a running simulation.

`server.py` streams the world to Unity. This package lets an agent framework —
OpenClaw, or anything else that speaks MCP — watch the same run and steer it at
the level of goals: rebalance the zones, work this region first, that machine
has broken down. It never drives a machine.

    tools.py     what the supervisor may ask and may change
    policy.py    the gate every command passes through, and the audit trail
    watcher.py   what wakes the agent when the fleet gets into trouble
    mcp.py       the MCP wire protocol, JSON-RPC over one HTTP endpoint
    http.py      just enough HTTP to carry it
"""

from .mcp import McpEndpoint, Tool
from .policy import Guard
from .tools import build_tools
from .watcher import Watcher

__all__ = ["McpEndpoint", "Tool", "Guard", "build_tools", "Watcher"]
