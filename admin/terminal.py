"""PTY-backed WebSocket bridge for /onboard (hermes setup) and /tui (hermes chat).

Each WebSocket session forks a child process inside a PTY. PTY output is
streamed to the browser via JSON envelope ``{"type":"output","data":"..."}``.
Browser input arrives as ``{"type":"input","data":"..."}`` and is written to
the PTY master fd; multi-character input is paced one char at a time so prompts
backed by @clack/prompts (used by Hermes setup) render correctly. Resize
events come as ``{"type":"resize","cols":N,"rows":N}``.

When the child exits with code 0 from /onboard, an "onboarded" marker is
written so the root redirect stops sending users back to the wizard.
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
ONBOARDED_MARKER = HERMES_HOME / ".hermes" / "onboarded"


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


def mark_onboarded() -> None:
    ONBOARDED_MARKER.parent.mkdir(parents=True, exist_ok=True)
    ONBOARDED_MARKER.write_text("ok\n", encoding="utf-8")


def is_onboarded() -> bool:
    return ONBOARDED_MARKER.exists()


async def _bridge(ws: WebSocket, argv: list[str], on_clean_exit=None) -> None:
    # Lazy import: ptyprocess only resolves inside the container.
    from ptyprocess import PtyProcessUnicode  # type: ignore

    cwd = str(HERMES_HOME / "workspace")
    child = PtyProcessUnicode.spawn(
        argv,
        cwd=cwd,
        env=_build_env(),
        dimensions=(30, 120),
    )
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
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        if text:
            out_queue.put_nowait(text)

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
                    cols = int(msg.get("cols") or 120)
                    rows = int(msg.get("rows") or 30)
                    _resize(fd, cols, rows)
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

        # Reap the child first so we know its exit status.
        exit_code = None
        try:
            if child.isalive():
                child.terminate(force=False)
                # Give it a moment, then SIGKILL if still alive.
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
            exit_code = None

        for task in (writer_task, reader_task):
            if not task.done():
                task.cancel()

        try:
            await ws.send_text(json.dumps({"type": "exit", "code": exit_code}))
        except (WebSocketDisconnect, RuntimeError):
            pass

        if exit_code == 0 and on_clean_exit is not None:
            try:
                on_clean_exit()
            except Exception as exc:
                print(f"[terminal] on_clean_exit failed: {exc!r}", flush=True)

        try:
            await ws.close()
        except RuntimeError:
            pass


def _cookie_authed(ws: WebSocket) -> bool:
    from .app import COOKIE_NAME, verify_signed_value

    return verify_signed_value(ws.cookies.get(COOKIE_NAME, ""))


async def onboard_ws(websocket: WebSocket) -> None:
    if not _cookie_authed(websocket):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await _bridge(
        websocket,
        ["/opt/hermes/.venv/bin/hermes", "setup"],
        on_clean_exit=mark_onboarded,
    )


async def tui_ws(websocket: WebSocket) -> None:
    if not _cookie_authed(websocket):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await _bridge(websocket, ["/opt/hermes/.venv/bin/hermes"])
