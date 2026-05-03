import contextlib
import hashlib
import hmac
import html
import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path
from string import Template
from urllib.parse import parse_qs

import yaml
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from . import proxy as hermes_proxy
from . import supervisor
from . import terminal as hermes_terminal


HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data"))
LOG_DIR = HERMES_HOME / "logs"
LOG_FILE = LOG_DIR / "gateway.log"
PID_FILE = HERMES_HOME / "gateway.pid"
ENV_FILE = HERMES_HOME / ".env"
CONFIG_FILE = HERMES_HOME / "config.yaml"
SKILLS_DIR = HERMES_HOME / "skills"
WORKSPACE_DIR = HERMES_HOME / "workspace"
HOME_DIR = HERMES_HOME / "home"
COOKIE_NAME = "hermes_admin"
COOKIE_MAX_AGE = 60 * 60 * 24 * 14

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

PROVIDER_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "kimi-coding": "https://api.kimi.com/coding/v1",
    "minimax": "https://api.minimax.io/v1",
}

PROVIDER_ENV_KEYS = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "zai": "GLM_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "huggingface": "HF_TOKEN",
    "custom": "OPENAI_API_KEY",
}

SECRET_FIELD_KEYS = {
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GLM_API_KEY",
    "KIMI_API_KEY",
    "MINIMAX_API_KEY",
    "HF_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "EMAIL_PASSWORD",
}


def escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def ensure_dirs() -> None:
    for path in (LOG_DIR, SKILLS_DIR, WORKSPACE_DIR, HOME_DIR):
        path.mkdir(parents=True, exist_ok=True)


def admin_secret() -> str:
    return ADMIN_PASSWORD or "change-me"


def sign_value(value: str) -> str:
    digest = hmac.new(admin_secret().encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}:{digest}"


def verify_signed_value(token: str) -> bool:
    if ":" not in token:
        return False
    value, digest = token.rsplit(":", 1)
    expected = hmac.new(admin_secret().encode(), value.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected) and value == ADMIN_USERNAME


def is_authenticated(request) -> bool:
    return verify_signed_value(request.cookies.get(COOKIE_NAME, ""))


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


async def parse_form(request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


NAV_HTML = """
<nav class="nav">
  <a href="/lite">Lite Panel</a>
  <a href="/onboard">Onboard</a>
  <a href="/tui">TUI</a>
  <a href="/hermes/">Hermes Dashboard</a>
  <a href="/setup">Setup</a>
  <a href="/logs">Logs</a>
  <form method="post" action="/logout"><button class="link-button" type="submit">Log out</button></form>
</nav>
"""


def render_page(title: str, content: str, *, authed: bool = False, message: str = "") -> HTMLResponse:
    nav = NAV_HTML if authed else ""

    template = Template(
        """
        <!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>$title</title>
            <link rel="stylesheet" href="/static/style.css">
          </head>
          <body>
            <main class="shell">
              <header class="hero">
                <div>
                  <p class="eyebrow">Railway template</p>
                  <h1>$title</h1>
                </div>
                $nav
              </header>
              $message
              $content
            </main>
          </body>
        </html>
        """
    )
    message_html = f'<div class="notice">{escape(message)}</div>' if message else ""
    return HTMLResponse(
        template.safe_substitute(
            title=escape(title),
            nav=nav,
            message=message_html,
            content=content,
        )
    )


def read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        try:
            values[key] = shlex.split(raw_value)[0] if raw_value else ""
        except ValueError:
            values[key] = raw_value.strip("\"'")
    return values


def quote_env_value(value: str) -> str:
    if value == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:@%+=,\-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env_updates(updates: dict[str, str]) -> None:
    ensure_dirs()
    ENV_FILE.touch(exist_ok=True)
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            output.append(f"{key}={quote_env_value(updates[key])}")
            seen.add(key)
        else:
            output.append(line)

    if output and output[-1].strip():
        output.append("")

    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={quote_env_value(value)}")

    ENV_FILE.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def save_config(config: dict) -> None:
    ensure_dirs()
    CONFIG_FILE.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    CONFIG_FILE.chmod(0o600)


def write_setup(form: dict[str, str]) -> None:
    updates: dict[str, str] = {}
    provider = form.get("provider", "openrouter").strip() or "openrouter"
    model = form.get("model", "").strip()
    custom_base_url = form.get("custom_base_url", "").strip()
    searxng_url = form.get("searxng_url", "").strip()

    for key in SECRET_FIELD_KEYS:
        value = form.get(key, "").strip()
        if value:
            updates[key] = value

    for key in (
        "TELEGRAM_ALLOWED_USERS",
        "DISCORD_ALLOWED_USERS",
        "SLACK_ALLOWED_USERS",
        "EMAIL_ADDRESS",
        "EMAIL_IMAP_HOST",
        "EMAIL_IMAP_PORT",
        "EMAIL_SMTP_HOST",
        "EMAIL_SMTP_PORT",
        "EMAIL_ALLOWED_USERS",
    ):
        value = form.get(key, "").strip()
        if value:
            updates[key] = value

    if searxng_url:
        updates["SEARXNG_URL"] = searxng_url
    if form.get("gateway_allow_all_users") == "true":
        updates["GATEWAY_ALLOW_ALL_USERS"] = "true"

    if updates:
        write_env_updates(updates)

    config = load_config()
    model_config = config.get("model")
    if not isinstance(model_config, dict):
        model_config = {"default": model_config} if model_config else {}
    if model:
        model_config["default"] = model
    model_config["provider"] = provider
    base_url = custom_base_url if provider == "custom" else PROVIDER_BASE_URLS.get(provider, "")
    if base_url:
        model_config["base_url"] = base_url
    elif "base_url" in model_config:
        model_config.pop("base_url", None)
    config["model"] = model_config

    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        skills = {}
        config["skills"] = skills
    load_cfg = skills.setdefault("load", {})
    if not isinstance(load_cfg, dict):
        load_cfg = {}
        skills["load"] = load_cfg
    extra_dirs = load_cfg.setdefault("extraDirs", [])
    if not isinstance(extra_dirs, list):
        extra_dirs = []
        load_cfg["extraDirs"] = extra_dirs
    skills_dir = str(SKILLS_DIR)
    if skills_dir not in extra_dirs:
        extra_dirs.append(skills_dir)

    save_config(config)


def pid_from_file() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def gateway_running() -> bool:
    pid = pid_from_file()
    if pid is None:
        return False
    if process_alive(pid):
        return True
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return False


def start_gateway() -> str:
    ensure_dirs()
    if gateway_running():
        return "Gateway is already running."

    LOG_FILE.touch(exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(HERMES_HOME),
            "HERMES_CWD": str(WORKSPACE_DIR),
            "HOME": str(HOME_DIR),
            "HERMES_ACCEPT_HOOKS": env.get("HERMES_ACCEPT_HOOKS", "1"),
            "PATH": f"/opt/hermes/.venv/bin:/data/.local/bin:{env.get('PATH', '')}",
        }
    )
    command = ["/opt/hermes/.venv/bin/hermes", "gateway", "run", "--replace"]
    with LOG_FILE.open("ab") as log:
        log.write(b"\n--- Starting Hermes gateway ---\n")
        process = subprocess.Popen(
            command,
            cwd=str(WORKSPACE_DIR),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    return f"Gateway started with PID {process.pid}."


def stop_gateway() -> str:
    pid = pid_from_file()
    if pid is None or not process_alive(pid):
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return "Gateway is not running."

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.time() + 12
    while time.time() < deadline:
        if not process_alive(pid):
            break
        time.sleep(0.3)

    if process_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return "Gateway stopped."


def tail_log(lines: int = 300) -> str:
    if not LOG_FILE.exists():
        return "Gateway log has not been created yet."
    data = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])


def current_summary() -> dict[str, str]:
    env_values = read_env_file()
    config = load_config()
    model_cfg = config.get("model", {})
    if not isinstance(model_cfg, dict):
        model_cfg = {"default": model_cfg or ""}
    provider = str(model_cfg.get("provider") or "auto")
    model = str(model_cfg.get("default") or model_cfg.get("model") or "")
    return {
        "provider": provider,
        "model": model,
        "searxng_url": env_values.get("SEARXNG_URL") or os.environ.get("SEARXNG_URL", ""),
        "telegram": "configured" if env_values.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") else "not configured",
        "discord": "configured" if env_values.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN") else "not configured",
        "slack": "configured" if env_values.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN") else "not configured",
    }


def selected(value: str, current: str) -> str:
    return "selected" if value == current else ""


def input_value(name: str, values: dict[str, str], default: str = "") -> str:
    value = values.get(name) or os.environ.get(name, default)
    return escape(value)


async def health(_request):
    summary = current_summary()
    return JSONResponse(
        {
            "ok": True,
            "gatewayRunning": gateway_running(),
            "webDashboardReady": supervisor.web_ready.is_set(),
            "configured": bool(summary["model"]),
            "searxngConfigured": bool(summary["searxng_url"]),
        }
    )


async def login_page(request):
    if is_authenticated(request):
        return redirect("/onboard" if not hermes_terminal.is_onboarded() else "/lite")
    message = request.query_params.get("message", "")
    content = """
    <section class="panel narrow">
      <form method="post" action="/login" class="stack">
        <label>Username <input name="username" autocomplete="username" required></label>
        <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
        <button type="submit">Log in</button>
      </form>
    </section>
    """
    return render_page("Hermes Agent Admin", content, message=message)


async def login(request):
    form = await parse_form(request)
    if form.get("username") == ADMIN_USERNAME and form.get("password") == ADMIN_PASSWORD:
        response = redirect("/onboard" if not hermes_terminal.is_onboarded() else "/lite")
        response.set_cookie(
            COOKIE_NAME,
            sign_value(ADMIN_USERNAME),
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=COOKIE_MAX_AGE,
        )
        return response
    return redirect("/login?message=Invalid%20credentials")


async def logout(_request):
    response = redirect("/login")
    response.delete_cookie(COOKIE_NAME)
    return response


async def root_redirect(request):
    if not is_authenticated(request):
        return redirect("/login")
    return redirect("/lite" if hermes_terminal.is_onboarded() else "/onboard")


async def dashboard(request):
    if not is_authenticated(request):
        return redirect("/login")
    summary = current_summary()
    status = "Running" if gateway_running() else "Stopped"
    web_status = "Ready" if supervisor.web_ready.is_set() else "Starting"
    content = f"""
    <section class="grid">
      <article class="card">
        <span class="label">Gateway</span>
        <strong>{escape(status)}</strong>
      </article>
      <article class="card">
        <span class="label">Web Dashboard</span>
        <strong>{escape(web_status)}</strong>
      </article>
      <article class="card">
        <span class="label">Provider</span>
        <strong>{escape(summary["provider"])}</strong>
      </article>
      <article class="card">
        <span class="label">Model</span>
        <strong>{escape(summary["model"] or "not configured")}</strong>
      </article>
      <article class="card">
        <span class="label">SearXNG</span>
        <strong>{escape("configured" if summary["searxng_url"] else "not configured")}</strong>
      </article>
    </section>

    <section class="panel">
      <h2>Gateway Controls</h2>
      <div class="actions">
        <form method="post" action="/gateway/start"><button type="submit">Start Gateway</button></form>
        <form method="post" action="/gateway/restart"><button type="submit">Restart Gateway</button></form>
        <form method="post" action="/gateway/stop"><button class="secondary" type="submit">Stop Gateway</button></form>
      </div>
      <p class="muted">Hermes runs as <code>hermes gateway run --replace</code> and writes logs to <code>{escape(LOG_FILE)}</code>.</p>
    </section>

    <section class="panel">
      <h2>Channels</h2>
      <ul class="status-list">
        <li>Telegram: {escape(summary["telegram"])}</li>
        <li>Discord: {escape(summary["discord"])}</li>
        <li>Slack: {escape(summary["slack"])}</li>
      </ul>
    </section>
    """
    return render_page("Hermes Agent Admin", content, authed=True, message=request.query_params.get("message", ""))


async def setup_page(request):
    if not is_authenticated(request):
        return redirect("/login")
    env_values = read_env_file()
    summary = current_summary()
    provider = summary["provider"]
    content = f"""
    <form method="post" action="/setup" class="panel form-grid">
      <h2>Model Provider</h2>
      <label>Provider
        <select name="provider">
          <option value="openrouter" {selected("openrouter", provider)}>OpenRouter</option>
          <option value="anthropic" {selected("anthropic", provider)}>Anthropic</option>
          <option value="openai" {selected("openai", provider)}>OpenAI</option>
          <option value="gemini" {selected("gemini", provider)}>Google Gemini</option>
          <option value="zai" {selected("zai", provider)}>z.ai / GLM</option>
          <option value="kimi-coding" {selected("kimi-coding", provider)}>Kimi</option>
          <option value="minimax" {selected("minimax", provider)}>MiniMax</option>
          <option value="huggingface" {selected("huggingface", provider)}>Hugging Face</option>
          <option value="custom" {selected("custom", provider)}>Custom OpenAI-compatible</option>
        </select>
      </label>
      <label>Default model <input name="model" value="{escape(summary["model"])}" placeholder="google/gemma-3-1b-it:free"></label>
      <label>Custom base URL <input name="custom_base_url" value="{escape(load_config().get("model", {}).get("base_url", "") if isinstance(load_config().get("model", {}), dict) else "")}" placeholder="https://api.example.com/v1"></label>

      <h2>Provider API Keys</h2>
      <label>OpenRouter API key <input name="OPENROUTER_API_KEY" type="password" placeholder="{escape('configured' if input_value('OPENROUTER_API_KEY', env_values) else '')}"></label>
      <label>Anthropic API key <input name="ANTHROPIC_API_KEY" type="password" placeholder="{escape('configured' if input_value('ANTHROPIC_API_KEY', env_values) else '')}"></label>
      <label>OpenAI API key <input name="OPENAI_API_KEY" type="password" placeholder="{escape('configured' if input_value('OPENAI_API_KEY', env_values) else '')}"></label>
      <label>Google API key <input name="GOOGLE_API_KEY" type="password" placeholder="{escape('configured' if input_value('GOOGLE_API_KEY', env_values) else '')}"></label>
      <label>z.ai / GLM API key <input name="GLM_API_KEY" type="password" placeholder="{escape('configured' if input_value('GLM_API_KEY', env_values) else '')}"></label>
      <label>Kimi API key <input name="KIMI_API_KEY" type="password" placeholder="{escape('configured' if input_value('KIMI_API_KEY', env_values) else '')}"></label>
      <label>MiniMax API key <input name="MINIMAX_API_KEY" type="password" placeholder="{escape('configured' if input_value('MINIMAX_API_KEY', env_values) else '')}"></label>
      <label>Hugging Face token <input name="HF_TOKEN" type="password" placeholder="{escape('configured' if input_value('HF_TOKEN', env_values) else '')}"></label>

      <h2>Search</h2>
      <label>SearXNG URL <input name="searxng_url" value="{input_value('SEARXNG_URL', env_values)}" placeholder="http://SearXNG.railway.internal:8080"></label>

      <h2>Messaging Channels</h2>
      <label>Telegram bot token <input name="TELEGRAM_BOT_TOKEN" type="password" placeholder="{escape('configured' if input_value('TELEGRAM_BOT_TOKEN', env_values) else '')}"></label>
      <label>Telegram allowed users <input name="TELEGRAM_ALLOWED_USERS" value="{input_value('TELEGRAM_ALLOWED_USERS', env_values)}" placeholder="123456789,*"></label>
      <label>Discord bot token <input name="DISCORD_BOT_TOKEN" type="password" placeholder="{escape('configured' if input_value('DISCORD_BOT_TOKEN', env_values) else '')}"></label>
      <label>Discord allowed users <input name="DISCORD_ALLOWED_USERS" value="{input_value('DISCORD_ALLOWED_USERS', env_values)}"></label>
      <label>Slack bot token <input name="SLACK_BOT_TOKEN" type="password" placeholder="{escape('configured' if input_value('SLACK_BOT_TOKEN', env_values) else '')}"></label>
      <label>Slack app token <input name="SLACK_APP_TOKEN" type="password" placeholder="{escape('configured' if input_value('SLACK_APP_TOKEN', env_values) else '')}"></label>
      <label>Slack allowed users <input name="SLACK_ALLOWED_USERS" value="{input_value('SLACK_ALLOWED_USERS', env_values)}"></label>

      <h2>Email</h2>
      <label>Email address <input name="EMAIL_ADDRESS" value="{input_value('EMAIL_ADDRESS', env_values)}"></label>
      <label>Email password <input name="EMAIL_PASSWORD" type="password" placeholder="{escape('configured' if input_value('EMAIL_PASSWORD', env_values) else '')}"></label>
      <label>IMAP host <input name="EMAIL_IMAP_HOST" value="{input_value('EMAIL_IMAP_HOST', env_values)}" placeholder="imap.gmail.com"></label>
      <label>IMAP port <input name="EMAIL_IMAP_PORT" value="{input_value('EMAIL_IMAP_PORT', env_values, '993')}"></label>
      <label>SMTP host <input name="EMAIL_SMTP_HOST" value="{input_value('EMAIL_SMTP_HOST', env_values)}" placeholder="smtp.gmail.com"></label>
      <label>SMTP port <input name="EMAIL_SMTP_PORT" value="{input_value('EMAIL_SMTP_PORT', env_values, '587')}"></label>
      <label>Email allowed users <input name="EMAIL_ALLOWED_USERS" value="{input_value('EMAIL_ALLOWED_USERS', env_values)}"></label>

      <label class="checkbox"><input type="checkbox" name="gateway_allow_all_users" value="true"> Allow all gateway users without allowlists</label>

      <button type="submit">Save Configuration</button>
    </form>
    """
    return render_page("Hermes Setup", content, authed=True, message=request.query_params.get("message", ""))


async def save_setup(request):
    if not is_authenticated(request):
        return redirect("/login")
    form = await parse_form(request)
    write_setup(form)
    return redirect("/setup?message=Configuration%20saved")


async def gateway_action(request):
    if not is_authenticated(request):
        return redirect("/login")
    action = request.path_params["action"]
    if action == "start":
        message = start_gateway()
    elif action == "stop":
        message = stop_gateway()
    elif action == "restart":
        stop_gateway()
        message = start_gateway()
    else:
        message = "Unknown gateway action."
    return redirect(f"/lite?message={message.replace(' ', '%20')}")


TERMINAL_TEMPLATE_PATH = Path(__file__).parent / "templates" / "onboard.html"


def _render_terminal(title: str, intro: str, ws_path: str) -> HTMLResponse:
    body = TERMINAL_TEMPLATE_PATH.read_text(encoding="utf-8")
    body = (
        body.replace("{{TITLE}}", escape(title))
        .replace("{{INTRO}}", escape(intro))
        .replace("{{WS_PATH}}", ws_path)
        .replace("{{NAV}}", NAV_HTML)
    )
    return HTMLResponse(body)


async def onboard_page(request):
    if not is_authenticated(request):
        return redirect("/login")
    return _render_terminal(
        "Hermes Onboarding",
        "Run the interactive `hermes setup` wizard. Choose a model provider, paste API keys, and configure messaging channels. Completion writes a marker so this page is no longer the default landing page.",
        "/onboard/ws",
    )


async def tui_page(request):
    if not is_authenticated(request):
        return redirect("/login")
    return _render_terminal(
        "Hermes TUI",
        "Interactive terminal session running `hermes`. Use it to chat, run skills, and inspect agent state. Closing the tab terminates the session.",
        "/tui/ws",
    )


async def logs_page(request):
    if not is_authenticated(request):
        return redirect("/login")
    text = escape(tail_log(400))
    content = f"""
    <section class="panel">
      <div class="actions spread">
        <h2>Gateway Logs</h2>
        <form method="post" action="/gateway/restart"><button type="submit">Restart Gateway</button></form>
      </div>
      <pre class="logs">{text}</pre>
    </section>
    """
    return render_page("Hermes Logs", content, authed=True)


PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

routes = [
    Route("/health", health, methods=["GET"]),
    Route("/login", login_page, methods=["GET"]),
    Route("/login", login, methods=["POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/", root_redirect, methods=["GET"]),
    Route("/lite", dashboard, methods=["GET"]),
    Route("/onboard", onboard_page, methods=["GET"]),
    WebSocketRoute("/onboard/ws", hermes_terminal.onboard_ws),
    Route("/tui", tui_page, methods=["GET"]),
    WebSocketRoute("/tui/ws", hermes_terminal.tui_ws),
    Mount(
        "/hermes",
        routes=[
            WebSocketRoute("/{path:path}", hermes_proxy.ws_proxy),
            Route("/", hermes_proxy.http_proxy, methods=PROXY_METHODS),
            Route("/{path:path}", hermes_proxy.http_proxy, methods=PROXY_METHODS),
        ],
    ),
    Route("/setup", setup_page, methods=["GET"]),
    Route("/setup", save_setup, methods=["POST"]),
    Route("/gateway/{action}", gateway_action, methods=["POST"]),
    Route("/logs", logs_page, methods=["GET"]),
    Mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static"),
]

ensure_dirs()


@contextlib.asynccontextmanager
async def lifespan(_app):
    await supervisor.start_web_dashboard()
    try:
        yield
    finally:
        await supervisor.stop_web_dashboard()


app = Starlette(debug=False, routes=routes, lifespan=lifespan)
