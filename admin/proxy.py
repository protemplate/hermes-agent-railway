"""Reverse proxy from /hermes/* to the native Hermes web dashboard at 127.0.0.1:9119.

Auth check (cookie) before opening the upstream connection. Hop-by-hop headers
are stripped; Host/Origin are rewritten to the loopback target; x-forwarded-*
and Via are dropped so the upstream sees a direct local request.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import httpx
import websockets
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import supervisor

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

DROPPED_RESPONSE_HEADERS = HOP_BY_HOP | {"content-encoding", "content-length"}

_client: httpx.AsyncClient | None = None


def _check_auth(request: Request) -> None:
    # Imported lazily to avoid a circular import with admin.app.
    from .app import is_authenticated

    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


def _filter_request_headers(headers) -> dict[str, str]:
    upstream = {k: v for k, v in headers.items() if k.lower() not in DROPPED_REQUEST_HEADERS}
    upstream["host"] = f"{supervisor.WEB_HOST}:{supervisor.WEB_PORT}"
    if "origin" in {k.lower() for k in upstream}:
        upstream = {k: v for k, v in upstream.items() if k.lower() != "origin"}
    upstream["origin"] = supervisor.WEB_BASE_URL
    return upstream


def _filter_response_headers(headers) -> list[tuple[str, str]]:
    return [(k, v) for k, v in headers.items() if k.lower() not in DROPPED_RESPONSE_HEADERS]


async def _ensure_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=supervisor.WEB_BASE_URL,
            timeout=httpx.Timeout(60.0, connect=5.0),
            follow_redirects=False,
        )
    return _client


async def http_proxy(request: Request) -> Response:
    _check_auth(request)
    if not supervisor.web_ready.is_set():
        return Response(
            "Hermes web dashboard is still starting. Try again in a moment.",
            status_code=503,
            media_type="text/plain",
        )

    path = "/" + request.path_params.get("path", "")
    if request.url.query:
        path = f"{path}?{request.url.query}"

    client = await _ensure_client()
    upstream_headers = _filter_request_headers(request.headers)

    body = await request.body()

    try:
        upstream = await client.request(
            request.method,
            path,
            headers=upstream_headers,
            content=body if body else None,
        )
    except httpx.ConnectError:
        return Response("Hermes web dashboard unavailable.", status_code=502, media_type="text/plain")

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=dict(_filter_response_headers(upstream.headers)),
        media_type=upstream.headers.get("content-type"),
    )


async def ws_proxy(websocket: WebSocket) -> None:
    # Mirror the cookie check used elsewhere — we only have access to the
    # raw cookie header on the ASGI scope, so reuse verify_signed_value directly.
    from .app import COOKIE_NAME, verify_signed_value

    cookies = websocket.cookies
    token = cookies.get(COOKIE_NAME, "")
    if not verify_signed_value(token):
        await websocket.close(code=4401)
        return

    if not supervisor.web_ready.is_set():
        await websocket.close(code=4503)
        return

    path = "/" + websocket.path_params.get("path", "")
    qs = websocket.scope.get("query_string", b"").decode()
    if qs:
        path = f"{path}?{qs}"

    upstream_url = "ws://" + f"{supervisor.WEB_HOST}:{supervisor.WEB_PORT}" + path
    subprotocols = websocket.scope.get("subprotocols") or None

    await websocket.accept(subprotocol=(subprotocols[0] if subprotocols else None))

    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=subprotocols,
            origin=supervisor.WEB_BASE_URL,
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
                {asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())},
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
