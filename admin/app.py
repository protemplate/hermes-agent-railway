"""Thin Starlette wrapper in front of hermes-webui.

Adds one new surface — `/auth-cli` — that exposes an in-browser xterm running
`hermes login --provider <X> --no-browser` for OAuth device-code providers
(Codex / Nous Portal). Every other path is reverse-proxied to hermes-webui on
127.0.0.1:9119, including WebSockets and SSE chat streams.

This wrapper does NOT enforce its own auth; it delegates to hermes-webui's
existing password gate. The /auth-cli WebSocket validates the
`hermes_session` cookie by probing hermes-webui's API.
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route, WebSocketRoute

from . import proxy as hermes_proxy
from . import terminal as hermes_terminal


TEMPLATE_PATH = Path(__file__).parent / "templates" / "auth_cli.html"


async def _is_authenticated(request: Request) -> bool:
    """Cheap auth probe: hit hermes-webui's /api/onboarding/status with the user's cookies.

    First call after boot can be slow (5+s) due to hermes_cli imports inside the
    webui server. Use a generous timeout — auth checks are infrequent.
    """
    import httpx

    cookie = request.headers.get("cookie", "")
    if not cookie:
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{hermes_proxy.WEBUI_BASE_URL}/api/onboarding/status",
                headers={"cookie": cookie, "host": f"{hermes_proxy.WEBUI_HOST}:{hermes_proxy.WEBUI_PORT}"},
            )
        return r.status_code == 200
    except (httpx.ConnectError, httpx.ReadTimeout):
        return False


async def auth_cli_page(request: Request):
    if not await _is_authenticated(request):
        return RedirectResponse("/login?next=/auth-cli", status_code=303)
    return HTMLResponse(TEMPLATE_PATH.read_text(encoding="utf-8"))


PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


routes = [
    Route("/auth-cli", auth_cli_page, methods=["GET"]),
    WebSocketRoute("/auth-cli/ws/{provider}", hermes_terminal.login_ws),
    # Catch-all proxy for everything else (HTTP + WebSocket).
    WebSocketRoute("/{path:path}", hermes_proxy.ws_proxy),
    Route("/{path:path}", hermes_proxy.http_proxy, methods=PROXY_METHODS),
    # Root path needs its own route — Starlette's path converter requires at least one segment.
    Route("/", hermes_proxy.http_proxy, methods=PROXY_METHODS),
]


app = Starlette(debug=False, routes=routes)
