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


_PROVIDER_BASE = {
    "openai-codex": "https://chatgpt.com/backend-api/codex",
    "nous": "https://inference-api.nousresearch.com/v1",
}

# Sensible default model per OAuth provider, used when we have to create
# config.yaml from scratch (after a Codex/Nous login on a fresh deploy where
# `hermes model` was never run). Picked to match the catalog's default and
# common Nous Portal subscription tiers.
_PROVIDER_DEFAULT_MODEL = {
    "openai-codex": "gpt-5.5",
    "nous": "Hermes-3-Llama-3.1-70B-FP8",
}


def _post_auth_heal_and_restart() -> str:
    """After a successful auth/model command, normalize state and restart hermes-webui.

    1. If config.yaml lacks model.provider but auth.json has a known OAuth credential,
       set model.provider + base_url so the agent can find credentials.
    2. Backfill stale `model: <other-provider>/<x>` and missing model_provider on
       webui sessions (and the _index.json cache).
    3. Delete the catalog cache file so the next webui process rebuilds fresh.
    4. SIGTERM hermes-webui — entrypoint's watchdog respawns it.

    Returns a short human-readable status line for the xterm.
    """
    import json
    import signal as _sig
    import subprocess as _sp

    try:
        import yaml
    except Exception as exc:
        return f"heal skipped (no yaml): {exc!r}"

    notes: list[str] = []
    home = HERMES_HOME

    # 1. Heal config.yaml — CREATE it if missing, or fill in missing model.provider
    auth_p = home / "auth.json"
    cfg_p = home / "config.yaml"
    chosen_provider: str | None = None
    if auth_p.exists():
        try:
            auth = json.loads(auth_p.read_text(encoding="utf-8")) or {}
            providers = (auth.get("providers") or {}) if isinstance(auth.get("providers"), dict) else {}
            authed = [p for p in providers.keys() if p in _PROVIDER_BASE]
            if authed:
                if cfg_p.exists():
                    cfg = yaml.safe_load(cfg_p.read_text(encoding="utf-8")) or {}
                else:
                    cfg = {}
                model_cfg = cfg.get("model")
                if not isinstance(model_cfg, dict):
                    model_cfg = {}
                if not model_cfg.get("provider"):
                    chosen_provider = authed[0]
                    model_cfg["provider"] = chosen_provider
                    if not model_cfg.get("base_url"):
                        model_cfg["base_url"] = _PROVIDER_BASE[chosen_provider]
                    if not model_cfg.get("default") and chosen_provider in _PROVIDER_DEFAULT_MODEL:
                        model_cfg["default"] = _PROVIDER_DEFAULT_MODEL[chosen_provider]
                    cfg["model"] = model_cfg
                    yaml.safe_dump(cfg, cfg_p.open("w", encoding="utf-8"), sort_keys=False)
                    notes.append(f"wrote config.yaml: model.provider={chosen_provider}, default={model_cfg.get('default')!r}")
                else:
                    chosen_provider = model_cfg["provider"]
        except Exception as exc:
            notes.append(f"config heal failed: {exc!r}")

    # 2. Backfill stale session models
    if chosen_provider:
        try:
            cfg_now = yaml.safe_load(cfg_p.read_text(encoding="utf-8")) or {}
            mc = cfg_now.get("model") or {}
            default_model = (mc.get("default") or "").strip()
            default_provider = (mc.get("provider") or "").strip()
            if default_model and default_provider:
                sessions_dir = home / ".hermes" / "webui" / "sessions"
                patched = 0
                if sessions_dir.exists():
                    for sf in sessions_dir.glob("*.json"):
                        if sf.name == "_index.json":
                            continue
                        try:
                            s = json.loads(sf.read_text(encoding="utf-8"))
                        except Exception:
                            continue
                        if not isinstance(s, dict):
                            continue
                        m = s.get("model") or ""
                        if not s.get("model_provider") or ("/" in m and not m.startswith(default_provider + "/")):
                            s["model"] = default_model
                            s["model_provider"] = default_provider
                            sf.write_text(json.dumps(s), encoding="utf-8")
                            patched += 1
                idx_p = sessions_dir / "_index.json"
                if idx_p.exists():
                    try:
                        idx = json.loads(idx_p.read_text(encoding="utf-8"))
                        for e in idx if isinstance(idx, list) else []:
                            if not isinstance(e, dict):
                                continue
                            m = e.get("model") or ""
                            if not e.get("model_provider") or ("/" in m and not m.startswith(default_provider + "/")):
                                e["model"] = default_model
                                e["model_provider"] = default_provider
                        idx_p.write_text(json.dumps(idx), encoding="utf-8")
                    except Exception:
                        pass
                if patched:
                    notes.append(f"backfilled {patched} sessions")
        except Exception as exc:
            notes.append(f"session heal failed: {exc!r}")

    # 3. Wipe catalog cache
    cache_p = home / ".hermes" / "webui" / "models_cache.json"
    try:
        if cache_p.exists():
            cache_p.unlink()
            notes.append("wiped catalog cache")
    except OSError:
        pass

    # 4. Restart webui — entrypoint's watchdog respawns it.
    pid_p = home / ".hermes" / "webui" / "server.pid"
    try:
        pid_text = pid_p.read_text(encoding="utf-8").strip() if pid_p.exists() else ""
        pid = int(pid_text) if pid_text else 0
        if pid > 0:
            os.kill(pid, _sig.SIGTERM)
            notes.append(f"sent SIGTERM to webui pid {pid}")
    except (OSError, ValueError):
        # Fallback: pkill by command line
        try:
            _sp.run(["pkill", "-TERM", "-f", "/opt/hermes-webui/server.py"], check=False)
            notes.append("sent pkill -TERM to webui")
        except Exception:
            pass

    return "; ".join(notes) if notes else "no-op (nothing to heal)"


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

        # On clean exit, normalize state and trigger a webui restart so the
        # user sees a fresh catalog/sessions on the next page load.
        heal_note = ""
        if exit_code == 0:
            try:
                heal_note = await asyncio.get_running_loop().run_in_executor(
                    None, _post_auth_heal_and_restart
                )
            except Exception as exc:
                heal_note = f"heal raised: {exc!r}"

        try:
            await ws.send_text(json.dumps({"type": "exit", "code": exit_code, "heal": heal_note}))
        except (WebSocketDisconnect, RuntimeError):
            pass

        try:
            await ws.close()
        except RuntimeError:
            pass
