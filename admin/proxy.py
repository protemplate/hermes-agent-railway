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

import httpx
import websockets
from starlette.requests import Request
from starlette.responses import StreamingResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect


WEBUI_HOST = "127.0.0.1"
WEBUI_PORT = 9119
WEBUI_BASE_URL = f"http://{WEBUI_HOST}:{WEBUI_PORT}"
WEBUI_WS_BASE = f"ws://{WEBUI_HOST}:{WEBUI_PORT}"


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
    if request.url.query:
        path = f"{path}?{request.url.query}"

    client = await _ensure_client()
    upstream_headers = _filter_request_headers(request.headers)

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
        return Response(
            "Hermes WebUI unavailable.",
            status_code=502,
            media_type="text/plain",
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
