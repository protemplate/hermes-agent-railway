"""The shared upstream client must never keep cookies, or one login authenticates every visitor.

Run from the repo root: python tests/test_proxy_cookies.py
"""
import asyncio
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from admin.proxy import no_cookie_jar  # noqa: E402


class Upstream(BaseHTTPRequestHandler):
    """Sets a session cookie on /login and echoes back whatever Cookie header it receives."""

    def do_GET(self):
        body = (self.headers.get("Cookie") or "").encode()
        self.send_response(200)
        if self.path == "/login":
            self.send_header("Set-Cookie", "hermes_session=owner; Path=/; HttpOnly")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def cookie_seen_after_login(port: int, visitor_cookie: str | None = None, **client_kwargs) -> str:
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", **client_kwargs) as client:
        await client.get("/login")  # the owner signs in through the shared client
        headers = {"cookie": visitor_cookie} if visitor_cookie else {}
        return (await client.get("/echo", headers=headers)).text  # a later visitor's request


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    assert asyncio.run(cookie_seen_after_login(port)) == "hermes_session=owner", "control: httpx default should leak"
    assert asyncio.run(cookie_seen_after_login(port, cookies=no_cookie_jar())) == "", "shared client leaked the cookie"
    own = asyncio.run(cookie_seen_after_login(port, "hermes_session=visitor", cookies=no_cookie_jar()))
    assert own == "hermes_session=visitor", f"a visitor's own cookie must pass through, got {own!r}"
    print("ok: shared upstream client stores no cookies")
