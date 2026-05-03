"""Background supervisor for the native Hermes web dashboard.

Spawns the hermes web command from the Starlette lifespan, restarts on exit
with a 5s backoff, and exposes a readiness flag for /health.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import httpx


HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data"))
WEB_LOG = HERMES_HOME / "logs" / "web.log"
WEB_PORT = int(os.environ.get("HERMES_WEB_PORT", "9119"))
WEB_HOST = "127.0.0.1"
WEB_BASE_URL = f"http://{WEB_HOST}:{WEB_PORT}"

web_ready = asyncio.Event()
_proc: asyncio.subprocess.Process | None = None
_watchdog_task: asyncio.Task | None = None
_ready_task: asyncio.Task | None = None


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(HERMES_HOME),
            "HOME": str(HERMES_HOME / "home"),
            "PATH": f"/opt/hermes/.venv/bin:{HERMES_HOME}/.local/bin:{env.get('PATH', '')}",
        }
    )
    return env


async def _spawn() -> asyncio.subprocess.Process:
    WEB_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = WEB_LOG.open("ab")
    log.write(b"\n--- Starting Hermes web dashboard ---\n")
    log.flush()
    return await asyncio.create_subprocess_exec(
        "/opt/hermes/.venv/bin/hermes",
        "web",
        "--no-open",
        "--host",
        WEB_HOST,
        "--port",
        str(WEB_PORT),
        cwd=str(HERMES_HOME / "workspace"),
        env=_build_env(),
        stdout=log,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )


async def _poll_ready() -> None:
    deadline = asyncio.get_running_loop().time() + 90
    async with httpx.AsyncClient(timeout=3.0) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                resp = await client.get(WEB_BASE_URL + "/")
                if resp.status_code < 500:
                    web_ready.set()
                    return
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
                pass
            await asyncio.sleep(1.0)


async def _watchdog() -> None:
    global _proc, _ready_task
    backoff = 1.0
    while True:
        try:
            _proc = await _spawn()
        except Exception as exc:
            print(f"[supervisor] failed to spawn hermes web: {exc!r}", flush=True)
            await asyncio.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)
            continue

        web_ready.clear()
        _ready_task = asyncio.create_task(_poll_ready())

        rc = await _proc.wait()
        web_ready.clear()
        if _ready_task and not _ready_task.done():
            _ready_task.cancel()
        print(f"[supervisor] hermes web exited with code {rc}, restarting in 5s", flush=True)
        await asyncio.sleep(5)


async def start_web_dashboard() -> None:
    global _watchdog_task
    if _watchdog_task and not _watchdog_task.done():
        return
    _watchdog_task = asyncio.create_task(_watchdog(), name="hermes-web-watchdog")


async def stop_web_dashboard() -> None:
    global _watchdog_task, _proc, _ready_task
    if _watchdog_task:
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass
        _watchdog_task = None
    if _ready_task:
        _ready_task.cancel()
        _ready_task = None
    if _proc and _proc.returncode is None:
        try:
            os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                _proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(_proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(_proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    _proc = None
    web_ready.clear()
