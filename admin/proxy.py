"""Reverse proxy: forwards `/*` (HTTP and WebSocket) to hermes-webui at 127.0.0.1:9119.

hermes-webui owns auth, session cookies, SSE chat streams, and almost the entire
public surface. Our wrapper sits in front purely so we can also serve `/auth-cli`
and its WebSocket route on the same public port.

Header treatment:
- Hop-by-hop headers stripped (connection, keep-alive, transfer-encoding, etc.).
- `Host` rewritten to the loopback target.
- `Origin` rewritten to the loopback target so hermes-webui's same-origin checks pass.
- `X-Forwarded-*`, `Forwarded`, `Via` dropped so the upstream sees a direct local request.

HTTP responses are streamed end-to-end (important for SSE chat).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import websockets
import yaml
from starlette.requests import Request
from starlette.responses import StreamingResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect


WEBUI_HOST = "127.0.0.1"
WEBUI_PORT = 9119
WEBUI_BASE_URL = f"http://{WEBUI_HOST}:{WEBUI_PORT}"
WEBUI_WS_BASE = f"ws://{WEBUI_HOST}:{WEBUI_PORT}"

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data"))
CONFIG_PATH = HERMES_HOME / "config.yaml"
AUTH_PATH = HERMES_HOME / "auth.json"
_config_provider_cache: tuple[float, str | None] | None = None
_oauth_cache: tuple[float, float, bool] | None = None


def _oauth_configured() -> bool:
    """True iff auth.json contains a provider matching config.yaml's model.provider.

    Used to suppress the WebUI onboarding wizard after `/auth-cli` completes:
    upstream's auto-complete (`config_auto_completed`) requires `chat_ready`,
    which requires `imports_ok`, which can briefly flicker False while the
    webui process restarts after our heal step. During that window the wizard
    re-renders. mtime-cached so this costs ~zero on the hot path.
    """
    global _oauth_cache
    try:
        cfg_mtime = CONFIG_PATH.stat().st_mtime
        auth_mtime = AUTH_PATH.stat().st_mtime
    except OSError:
        return False
    if _oauth_cache and _oauth_cache[0] == cfg_mtime and _oauth_cache[1] == auth_mtime:
        return _oauth_cache[2]
    ok = False
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        auth = json.loads(AUTH_PATH.read_text(encoding="utf-8")) or {}
        providers = auth.get("providers") if isinstance(auth.get("providers"), dict) else {}
        model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
        prov = (model_cfg.get("provider") or "").strip()
        ok = bool(prov and prov in providers)
    except (OSError, yaml.YAMLError, json.JSONDecodeError):
        ok = False
    _oauth_cache = (cfg_mtime, auth_mtime, ok)
    return ok


def _active_provider() -> str | None:
    """Return model.provider from config.yaml, with mtime-based caching.

    Workaround for hermes-webui upstream bug: when /api/session/new is called
    with no model_provider, the server stores model_provider=null on the new
    session even when catalog.active_provider is correctly set. The agent then
    can't determine credentials at chat-start time. We patch the request body
    to inject the provider before forwarding.
    """
    global _config_provider_cache
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return None
    if _config_provider_cache and _config_provider_cache[0] == mtime:
        return _config_provider_cache[1]
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    model_cfg = cfg.get("model")
    provider = (model_cfg or {}).get("provider") if isinstance(model_cfg, dict) else None
    if isinstance(provider, str):
        provider = provider.strip() or None
    else:
        provider = None
    _config_provider_cache = (mtime, provider)
    return provider


HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

DROPPED_REQUEST_HEADERS = HOP_BY_HOP | {
    "host",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-server",
    "x-real-ip",
    "forwarded",
    "via",
}

# Keep `content-encoding` so the client knows the body is gzipped; we forward
# the raw bytes via aiter_raw() and don't re-encode. Drop content-length because
# StreamingResponse sets `Transfer-Encoding: chunked` which conflicts.
DROPPED_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length"}


_client: httpx.AsyncClient | None = None


def _filter_request_headers(headers) -> dict[str, str]:
    upstream = {k: v for k, v in headers.items() if k.lower() not in DROPPED_REQUEST_HEADERS}
    upstream["host"] = f"{WEBUI_HOST}:{WEBUI_PORT}"
    upstream = {k: v for k, v in upstream.items() if k.lower() != "origin"}
    upstream["origin"] = WEBUI_BASE_URL
    return upstream


def _filter_response_headers(headers) -> list[tuple[str, str]]:
    return [(k, v) for k, v in headers.items() if k.lower() not in DROPPED_RESPONSE_HEADERS]


async def _ensure_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=WEBUI_BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0),
            follow_redirects=False,
        )
    return _client


async def http_proxy(request: Request) -> Response:
    raw_path = request.path_params.get("path", "")
    path = "/" + raw_path if not raw_path.startswith("/") else raw_path
    bare_path = path.split("?", 1)[0]
    if request.url.query:
        path = f"{path}?{request.url.query}"

    client = await _ensure_client()
    upstream_headers = _filter_request_headers(request.headers)

    body = await request.body()

    # Workaround for upstream hermes-webui: new sessions are persisted with
    # model_provider=null even when catalog.active_provider is set. Inject
    # model_provider from /data/config.yaml when the client doesn't supply one.
    if request.method == "POST" and bare_path == "/api/session/new":
        provider = _active_provider()
        if provider:
            try:
                payload = json.loads(body) if body else {}
                if isinstance(payload, dict) and not (payload.get("model_provider") or "").strip():
                    payload["model_provider"] = provider
                    body = json.dumps(payload).encode("utf-8")
                    upstream_headers = {
                        k: v for k, v in upstream_headers.items() if k.lower() != "content-length"
                    }
                    upstream_headers["content-type"] = "application/json"
            except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
                pass

    try:
        req = client.build_request(
            request.method,
            path,
            headers=upstream_headers,
            content=body if body else None,
        )
        upstream = await client.send(req, stream=True)
    except httpx.ConnectError:
        return Response(
            "Hermes WebUI unavailable.",
            status_code=502,
            media_type="text/plain",
        )

    # Workaround for upstream wizard race: when OAuth is configured but the
    # webui process is mid-restart (chat_ready=False because imports flicker),
    # `/api/onboarding/status` returns completed=false and the wizard re-renders.
    # Buffer the response and force completed=true when auth.json proves OAuth
    # is set up. Only buffers this single small JSON endpoint — every other
    # response (SSE chat, large payloads) streams unchanged.
    if (
        request.method == "GET"
        and bare_path == "/api/onboarding/status"
        and upstream.status_code == 200
        and _oauth_configured()
    ):
        try:
            raw = await upstream.aread()
            await upstream.aclose()
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("completed") is not True:
                payload["completed"] = True
            mutated = json.dumps(payload).encode("utf-8")
            headers = [
                (k, v)
                for k, v in _filter_response_headers(upstream.headers)
                if k.lower() not in ("content-encoding",)
            ]
            return Response(
                content=mutated,
                status_code=upstream.status_code,
                headers=dict(headers),
                media_type="application/json",
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass  # fall through to streaming the original body

    async def body_iter():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=dict(_filter_response_headers(upstream.headers)),
        media_type=upstream.headers.get("content-type"),
    )


async def ws_proxy(websocket: WebSocket) -> None:
    raw_path = websocket.path_params.get("path", "")
    path = "/" + raw_path if not raw_path.startswith("/") else raw_path
    qs = websocket.scope.get("query_string", b"").decode()
    if qs:
        path = f"{path}?{qs}"
    upstream_url = WEBUI_WS_BASE + path
    subprotocols = websocket.scope.get("subprotocols") or None

    await websocket.accept(subprotocol=(subprotocols[0] if subprotocols else None))

    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=subprotocols,
            origin=WEBUI_BASE_URL,
            open_timeout=5,
            ping_interval=None,
        ) as upstream:

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            return
                        if "text" in msg and msg["text"] is not None:
                            await upstream.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await upstream.send(msg["bytes"])
                except (WebSocketDisconnect, websockets.ConnectionClosed):
                    return

            async def upstream_to_client() -> None:
                try:
                    async for msg in upstream:
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except websockets.ConnectionClosed:
                    return

            done, pending = await asyncio.wait(
                {
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                },
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except (websockets.WebSocketException, OSError):
        pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass
