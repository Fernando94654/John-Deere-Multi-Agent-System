"""The MCP wire protocol: JSON-RPC 2.0 over one HTTP endpoint.

Only the request/response half of Streamable HTTP is implemented. The server
never needs to push anything at the client on its own — it is asked questions
and it answers them — so the SSE stream a client may open with `GET` is
declined, which the transport allows.

Four methods carry everything: `initialize`, `notifications/initialized`,
`tools/list` and `tools/call`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .http import json_response

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = PROTOCOL_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass(frozen=True)
class Tool:
    """One callable the supervisor is allowed to reach for."""

    name: str
    description: str
    schema: dict
    run: Callable[[dict], Any]
    #: True when the call changes the world rather than just reporting on it.
    mutating: bool = False
    #: True when calling it twice running is the same as calling it once.
    idempotent: bool = False
    #: True when it takes something away that cannot simply be put back.
    destructive: bool = False

    def describe(self) -> dict:
        # The hints are what let a client stop asking the operator to approve a
        # question. Without them every call is treated as if it might break
        # something, and reading the fleet state needs a click.
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": {
                "readOnlyHint": not self.mutating,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent or not self.mutating,
                "openWorldHint": False,
            },
        }


class McpEndpoint:
    """Turns HTTP requests into tool calls and back."""

    def __init__(self, tools: list[Tool], name: str, version: str = "1.0.0"):
        self.tools = {tool.name: tool for tool in tools}
        self.name = name
        self.version = version
        self.session_id = uuid.uuid4().hex

    # --- HTTP ------------------------------------------------------------
    async def handle(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """Route one HTTP request. Anything but a POST of JSON-RPC is refused."""
        if not path.rstrip("/").endswith("/mcp") and path.rstrip("/") not in ("", "/mcp"):
            return json_response(404, {"error": f"no endpoint at {path}"})
        if method == "GET":
            # No server-initiated stream on offer; the client falls back to POST.
            return 405, {"Allow": "POST, DELETE"}, b""
        if method == "DELETE":
            return 200, {}, b""
        if method != "POST":
            return 405, {"Allow": "POST"}, b""

        try:
            message = json.loads(body or b"null")
        except json.JSONDecodeError as error:
            return json_response(400, self._error(None, PARSE_ERROR, str(error)))

        extra = {"Mcp-Session-Id": self.session_id}
        if isinstance(message, list):
            # A batch: notifications drop out, so an all-notification batch
            # answers with no body at all.
            replies = [r for r in (await self._dispatch(m) for m in message) if r]
            if not replies:
                return 202, extra, b""
            status, made, payload = json_response(200, replies)
            return status, {**made, **extra}, payload

        reply = await self._dispatch(message)
        if reply is None:
            return 202, extra, b""
        status, made, payload = json_response(200, reply)
        return status, {**made, **extra}, payload

    # --- JSON-RPC --------------------------------------------------------
    async def _dispatch(self, message: Any) -> Optional[dict]:
        """Answer one JSON-RPC message; `None` for a notification."""
        if not isinstance(message, dict):
            return self._error(None, INVALID_REQUEST, "expected a JSON-RPC object")

        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        # A message with no id is a notification: acknowledged, never answered.
        notification = "id" not in message

        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
            return self._result(
                request_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                },
            )

        if method in ("notifications/initialized", "notifications/cancelled"):
            return None

        if method == "ping":
            return self._result(request_id, {})

        if method == "tools/list":
            return self._result(
                request_id,
                {"tools": [tool.describe() for tool in self.tools.values()]},
            )

        if method == "tools/call":
            if notification:
                return None
            return await self._call(request_id, params)

        if notification:
            return None
        return self._error(request_id, METHOD_NOT_FOUND, f"unknown method {method!r}")

    async def _call(self, request_id: Any, params: dict) -> dict:
        """Run one tool and wrap whatever it says in MCP's content envelope."""
        name = params.get("name")
        tool = self.tools.get(name)
        if tool is None:
            return self._error(request_id, INVALID_PARAMS, f"unknown tool {name!r}")

        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._error(request_id, INVALID_PARAMS, "arguments must be an object")

        try:
            outcome = tool.run(arguments)
        except Exception as error:
            # A refused or malformed call is an outcome the model should read
            # and correct, not a transport failure, so it comes back as a tool
            # error rather than a JSON-RPC one.
            reason = (
                str(error)
                if isinstance(error, ValueError)
                else f"{type(error).__name__}: {error}"
            )
            return self._result(
                request_id, self._content({"ok": False, "error": reason}, is_error=True)
            )

        return self._result(request_id, self._content(outcome))

    @staticmethod
    def _content(payload: Any, is_error: bool = False) -> dict:
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    @staticmethod
    def _result(request_id: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
