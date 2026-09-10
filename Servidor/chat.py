"""Stream the farm-manager reply through the local OpenClaw HTTP gateway."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import httpx
from starlette.responses import JSONResponse, StreamingResponse

BRIEF = (
    "Responde en el idioma del usuario, en 1 a 3 frases salvo que pida detalle. "
    "Usa solo las herramientas necesarias; no repitas consultas sin motivo. "
    "Conserva las verificaciones necesarias antes de modificar la simulación."
)


def gateway_settings():
    # Docker sets OPENCLAW_GATEWAY_HOST/PORT/TOKEN directly since the container has
    # no local OpenClaw config file; a native run falls back to reading it.
    host = os.environ.get("OPENCLAW_GATEWAY_HOST", "127.0.0.1")
    port = os.environ.get("OPENCLAW_GATEWAY_PORT")
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_PASSWORD")
    if port is None or not token:
        path = Path(os.environ.get("OPENCLAW_CONFIG_PATH", "~/.openclaw/openclaw.json")).expanduser()
        config = json.loads(path.read_text())
        gateway = config.get("gateway", {})
        auth = gateway.get("auth", {})
        port = port or gateway.get("port", 18789)
        token = token or auth.get("token") or auth.get("password")
    if not isinstance(token, str) or not token:
        raise ValueError("Configure a gateway credential on the server")
    return f"http://{host}:{int(port)}/v1/chat/completions", token


def event(kind, **data):
    return json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n"


class OpenClawChat:
    def __init__(self):
        self.active = set()

    async def handle(self, request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 20000:
                return self.error("El mensaje es demasiado largo.", 413)
        try:
            body = json.loads(raw)
            message = body["message"]
            conversation = str(UUID(body["conversationId"]))
            if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            return self.error("Escribe un mensaje de 1 a 4000 caracteres.", 400)
        try:
            url, token = gateway_settings()
        except (OSError, ValueError, TypeError):
            return self.error("Revisa la configuración del gateway OpenClaw en el servidor.", 503)
        if conversation in self.active or len(self.active) >= 4:
            return self.error("El asistente está ocupado. Espera un momento antes de enviar.", 429)
        self.active.add(conversation)

        async def stream():
            try:
                # Keep the previous CLI session key so existing conversations continue.
                headers = {"Authorization": f"Bearer {token}",
                           "x-openclaw-session-key": f"agent:farm-manager:web-simulation-{conversation}"}
                payload = {"model": "openclaw/farm-manager", "stream": True,
                           "messages": [{"role": "system", "content": BRIEF},
                                        {"role": "user", "content": message.strip()}]}
                async with asyncio.timeout(130), httpx.AsyncClient(timeout=125, trust_env=False) as client:
                    async with client.stream("POST", url, headers=headers, json=payload) as response:
                        if response.status_code != 200:
                            yield event("error", error="OpenClaw no pudo atender la consulta. Revisa el gateway antes de repetir una acción.")
                            return
                        has_text = False
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                if has_text:
                                    yield event("done")
                                else:
                                    yield event("error", error="OpenClaw no devolvió texto. Revisa la simulación antes de repetir una acción.")
                                return
                            chunk = json.loads(data)
                            if chunk.get("error"):
                                yield event("error", error="La respuesta se interrumpió. Revisa la simulación antes de repetir una acción.")
                                return
                            for choice in chunk.get("choices", []):
                                text = choice.get("delta", {}).get("content")
                                if isinstance(text, str) and text:
                                    has_text = True
                                    yield event("delta", text=text)
                        yield event("error", error="Se perdió la conexión antes de completar la respuesta. Revisa la simulación antes de repetir una acción.")
            except (TimeoutError, httpx.TimeoutException):
                yield event("error", error="La consulta tardó demasiado. OpenClaw podría seguir trabajando; revisa la simulación antes de repetir una acción.")
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                yield event("error", error="No se pudo completar la conexión con OpenClaw. Revisa la simulación antes de repetir una acción.")
            finally:
                self.active.discard(conversation)

        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @staticmethod
    def error(message, status):
        return JSONResponse({"error": message}, status_code=status, headers={"Cache-Control": "no-store"})
