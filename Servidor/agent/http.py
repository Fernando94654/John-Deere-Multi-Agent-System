"""A very small HTTP/1.1 server, enough to carry MCP over Streamable HTTP.

The engine has no web framework and does not want one. What the supervisor
needs is a single endpoint that accepts a JSON body and answers with one, so
this is about as much HTTP as that takes: a request line, headers, a
`Content-Length` body, and keep-alive so a client may reuse the connection.

It runs on the same event loop as the WebSocket bridge, which is the whole
point — a tool call and the tick loop touch the same `Session` object without
locks or a second copy of the world.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

#: method, path, headers (lowercased keys), body -> status, headers, body
Handler = Callable[
    [str, str, dict[str, str], bytes], Awaitable[tuple[int, dict[str, str], bytes]]
]

MAX_BODY = 4 * 1024 * 1024
MAX_HEADERS = 64 * 1024

REASONS = {
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    500: "Internal Server Error",
}


def json_response(status: int, payload) -> tuple[int, dict[str, str], bytes]:
    """Shape a JSON body into what a `Handler` has to return."""
    body = json.dumps(payload).encode()
    return status, {"Content-Type": "application/json"}, body


async def _read_request(
    reader: asyncio.StreamReader,
) -> Optional[tuple[str, str, dict[str, str], bytes]]:
    """Parse one request off the stream; `None` once the client is done."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
        return None
    if len(head) > MAX_HEADERS:
        return None

    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split()
    if len(parts) < 2:
        return None
    method, target = parts[0].upper(), parts[1]

    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if value:
            headers[name.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0") or 0)
    if length > MAX_BODY:
        return None
    body = await reader.readexactly(length) if length else b""
    return method, urlsplit(target).path, headers, body


async def serve_http(handler: Handler, host: str, port: int) -> asyncio.AbstractServer:
    """Start an HTTP server that answers every request through `handler`."""

    async def on_connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                request = await _read_request(reader)
                if request is None:
                    return
                method, path, headers, body = request

                try:
                    status, extra, payload = await handler(method, path, headers, body)
                except Exception as error:  # a tool crash must not kill the server
                    status, extra, payload = json_response(
                        500, {"error": f"{type(error).__name__}: {error}"}
                    )

                keep_alive = headers.get("connection", "").lower() != "close"
                head = [f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}"]
                head.append(f"Content-Length: {len(payload)}")
                head.append("Connection: keep-alive" if keep_alive else "Connection: close")
                head.extend(f"{name}: {value}" for name, value in extra.items())
                writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
                writer.write(payload)
                await writer.drain()

                if not keep_alive:
                    return
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()

    return await asyncio.start_server(on_connection, host, port)


async def post_json(url: str, payload: dict, token: Optional[str] = None) -> int:
    """POST `payload` as JSON and return the status code; 0 if it never landed.

    Blocking `urllib` on a worker thread: the supervisor is woken a handful of
    times per campaign, so a thread hop costs nothing and saves a dependency.
    """
    import urllib.error
    import urllib.request

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
