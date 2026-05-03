"""PTY+WebSocket bridge for /auth-cli — runs `hermes auth add <X> --type oauth --no-browser`.

Whitelist of allowed providers. Each WebSocket session forks one child in a PTY,
streams output to the browser as JSON envelopes, accepts input/resize messages.
On disconnect or child exit, the PTY child is reaped.

Hermes uses OAuth device-code (RFC 8628) for `openai-codex` and `nous`: the CLI
prints "Visit URL X, enter code Y", the user opens X in any browser (any device),
and the CLI polls until authorization completes. No localhost callback required.
The `--no-browser` flag suppresses the auto-open attempt that would fail in a
headless container anyway.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import struct
import termios
from pathlib import Path

from starlette.websockets import WebSocket, WebSocketDisconnect


HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data"))
HERMES_BIN = "/opt/hermes/.venv/bin/hermes"

ALLOWED_PROVIDERS = {"nous", "openai-codex"}


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(HERMES_HOME),
            "HOME": str(HERMES_HOME / "home"),
            "PATH": f"/opt/hermes/.venv/bin:{HERMES_HOME}/.local/bin:{env.get('PATH', '')}",
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "FORCE_COLOR": "1",
        }
    )
    return env


def _resize(fd: int, cols: int, rows: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


async def login_ws(websocket: WebSocket) -> None:
    provider = websocket.path_params.get("provider", "")

    if not await _check_auth(websocket):
        await websocket.close(code=4401)
        return

    if provider == "model":
        # Interactive provider picker — covers all providers including OAuth ones.
        argv = [HERMES_BIN, "model", "--no-browser"]
    elif provider in ALLOWED_PROVIDERS:
        argv = [HERMES_BIN, "auth", "add", provider, "--type", "oauth", "--no-browser"]
    else:
        await websocket.close(code=4400)
        return

    await websocket.accept()
    await _bridge(websocket, argv)


async def _check_auth(websocket: WebSocket) -> bool:
    """Validate the hermes_session cookie by hitting an authenticated WebUI endpoint.

    hermes-webui has no dedicated /api/auth/check, so we probe /api/onboarding/status
    which (a) requires auth when a password is configured and (b) is cheap.
    """
    import httpx

    cookie_header = websocket.headers.get("cookie", "")
    if not cookie_header:
        return False

    from . import proxy

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{proxy.WEBUI_BASE_URL}/api/onboarding/status",
                headers={"cookie": cookie_header, "host": f"{proxy.WEBUI_HOST}:{proxy.WEBUI_PORT}"},
            )
        return r.status_code == 200
    except (httpx.ConnectError, httpx.ReadTimeout):
        return False


async def _bridge(ws: WebSocket, argv: list[str]) -> None:
    from ptyprocess import PtyProcessUnicode  # type: ignore

    cwd = str(HERMES_HOME)
    child = PtyProcessUnicode.spawn(argv, cwd=cwd, env=_build_env(), dimensions=(30, 120))
    fd = child.fd
    loop = asyncio.get_running_loop()
    out_queue: asyncio.Queue[str | None] = asyncio.Queue()

    def _on_readable() -> None:
        try:
            data = os.read(fd, 4096)
        except OSError:
            loop.remove_reader(fd)
            out_queue.put_nowait(None)
            return
        if not data:
            loop.remove_reader(fd)
            out_queue.put_nowait(None)
            return
        out_queue.put_nowait(data.decode("utf-8", errors="replace"))

    loop.add_reader(fd, _on_readable)

    async def writer() -> None:
        while True:
            chunk = await out_queue.get()
            if chunk is None:
                return
            try:
                await ws.send_text(json.dumps({"type": "output", "data": chunk}))
            except (WebSocketDisconnect, RuntimeError):
                return

    async def reader() -> None:
        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type")
                if kind == "input":
                    data = msg.get("data", "")
                    if not data:
                        continue
                    if len(data) > 1:
                        for ch in data:
                            try:
                                os.write(fd, ch.encode("utf-8"))
                            except OSError:
                                return
                            await asyncio.sleep(0.005)
                    else:
                        try:
                            os.write(fd, data.encode("utf-8"))
                        except OSError:
                            return
                elif kind == "resize":
                    _resize(fd, int(msg.get("cols") or 120), int(msg.get("rows") or 30))
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            return

    writer_task = asyncio.create_task(writer())
    reader_task = asyncio.create_task(reader())

    try:
        await asyncio.wait({writer_task, reader_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        try:
            loop.remove_reader(fd)
        except (ValueError, KeyError):
            pass

        exit_code = None
        try:
            if child.isalive():
                child.terminate(force=False)
                for _ in range(20):
                    if not child.isalive():
                        break
                    await asyncio.sleep(0.1)
                if child.isalive():
                    child.kill(9)
            try:
                child.wait()
            except Exception:
                pass
            exit_code = child.exitstatus
        except Exception:
            pass

        for task in (writer_task, reader_task):
            if not task.done():
                task.cancel()

        try:
            await ws.send_text(json.dumps({"type": "exit", "code": exit_code}))
        except (WebSocketDisconnect, RuntimeError):
            pass

        try:
            await ws.close()
        except RuntimeError:
            pass
