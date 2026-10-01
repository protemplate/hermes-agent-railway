"""Reverse proxy for the upstream ``hermes dashboard`` CLI server (FastAPI SPA).

Hermes listens on loopback (see ``DASHBOARD_PORT`` / env) and is typically started manually from
`/tui`. The public Railway port only reaches this wrapper; traffic under
``DASHBOARD_MOUNT_PREFIX`` forwards to localhost with ``X-Forwarded-Prefix`` set so the Hermes
SPA rewrites asset URLs correctly (upstream ``mount_spa`` in ``hermes_cli/web_server.py``).
"""

from __future__ import annotations

import asyncio
import os

import httpx
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .proxy import (
    PROXY_METHODS,
    _filter_response_headers,
    filter_upstream_request_headers,
    no_cookie_jar,
)


def _dash_listen_port(raw: str | None, default: int = 9119) -> int:
    if not raw:
        return default
    try:
        p = int(str(raw).strip(), 10)
        return p if 1 <= p <= 65535 else default
    except ValueError:
        return default


def _validated_mount_prefix(raw: str) -> str:
    """Return mount path starting with '/', no trailing slash (except '/').

    Rejects traversal / odd characters similarly to Hermes ``_normalise_prefix``.
    """
    p = raw.strip()
    if not p or p == "/":
        raise ValueError("HERMES_DASHBOARD_MOUNT_PATH must be a non-root path segment")
    if not p.startswith("/"):
        p = "/" + p
    forwarded = p.rstrip("/") or ""
    if not forwarded:
        raise ValueError("invalid HERMES_DASHBOARD_MOUNT_PATH")
    # Forwarded-prefix form: ``/foo`` — no traversal
    rest = forwarded[1:]
    if "//" in forwarded or ".." in forwarded.split("/"):
        raise ValueError("HERMES_DASHBOARD_MOUNT_PATH contains invalid segments")
    if len(forwarded) > 64:
        raise ValueError("HERMES_DASHBOARD_MOUNT_PATH too long")
    for ch in '|"<> \n\r\t':
        if ch in rest:
            raise ValueError("HERMES_DASHBOARD_MOUNT_PATH contains forbidden characters")
    return forwarded


try:
    DASHBOARD_MOUNT_PREFIX = _validated_mount_prefix(
        os.environ.get("HERMES_DASHBOARD_MOUNT_PATH", "/hermes-dashboard"),
    )
except ValueError:
    DASHBOARD_MOUNT_PREFIX = "/hermes-dashboard"

DASHBOARD_HOST = (os.environ.get("HERMES_DASHBOARD_HOST") or "127.0.0.1").strip() or "127.0.0.1"
DASHBOARD_PORT = _dash_listen_port(os.environ.get("HERMES_DASHBOARD_PORT"))
DASHBOARD_BASE_URL = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"
DASHBOARD_WS_BASE = f"ws://{DASHBOARD_HOST}:{DASHBOARD_PORT}"

_dashboard_client: httpx.AsyncClient | None = None

_DASH_UNAVAILABLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width"/>
<title>Hermes dashboard unavailable</title>
</head>
<body>
<h1>502 — Hermes dashboard is not running</h1>
<p>The official <code>hermes dashboard</code> server is not listening on the container
loopback port yet (default <code>127.0.0.1:9119</code>).</p>
<p>Open the <a href="/tui">web terminal (/tui)</a> and run for example:</p>
<pre style="background:#f6f8fa;padding:.75rem 1rem;">hermes dashboard --no-open</pre>
<p>Then reload this URL. Customize the CLI bind with
<code>HERMES_DASHBOARD_HOST</code> / <code>HERMES_DASHBOARD_PORT</code>; the public path stays
<code>{}</code> on this Railway service.</p>
</body>
</html>"""


async def _ensure_dashboard_client() -> httpx.AsyncClient:
    global _dashboard_client
    if _dashboard_client is None:
        _dashboard_client = httpx.AsyncClient(
            base_url=DASHBOARD_BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0),
            follow_redirects=False,
            cookies=no_cookie_jar(),
        )
    return _dashboard_client


async def dashboard_http_proxy(request: Request) -> Response:
    path = request.url.path or "/"
    if request.url.query:
        path = f"{path}?{request.url.query}"

    upstream_headers = filter_upstream_request_headers(
        request.headers,
        upstream_host=DASHBOARD_HOST,
        upstream_port=DASHBOARD_PORT,
        forwarded_prefix=DASHBOARD_MOUNT_PREFIX,
    )

    client = await _ensure_dashboard_client()
    body = await request.body()
    try:
        req = client.build_request(
            request.method,
            path,
            headers=upstream_headers,
            content=body if body else None,
        )
        upstream = await client.send(req, stream=True)
    except httpx.ConnectError:
        return HTMLResponse(
            _DASH_UNAVAILABLE_HTML.format(DASHBOARD_MOUNT_PREFIX),
            status_code=502,
        )

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


async def dashboard_ws_proxy(websocket: WebSocket) -> None:
    raw_path = websocket.path_params.get("path", "")
    path = "/" + raw_path if not raw_path.startswith("/") else raw_path
    qs = websocket.scope.get("query_string", b"").decode()
    if qs:
        path = f"{path}?{qs}"
    upstream_url = DASHBOARD_WS_BASE + path
    subprotocols = websocket.scope.get("subprotocols") or None

    extra_headers = [("x-forwarded-prefix", DASHBOARD_MOUNT_PREFIX)]

    await websocket.accept(subprotocol=(subprotocols[0] if subprotocols else None))

    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=subprotocols,
            origin=DASHBOARD_BASE_URL,
            open_timeout=5,
            ping_interval=None,
            additional_headers=extra_headers,
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


def build_dashboard_starlette_app() -> Starlette:
    """Mounted under ``Mount(DASHBOARD_MOUNT_PREFIX, ...)``."""

    dash_routes = [
        Route("/", dashboard_http_proxy, methods=PROXY_METHODS),
        WebSocketRoute("/{path:path}", dashboard_ws_proxy),
        Route("/{path:path}", dashboard_http_proxy, methods=PROXY_METHODS),
    ]
    return Starlette(debug=False, routes=dash_routes)
