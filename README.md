# Hermes Agent Railway Template

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) (NousResearch's self-improving AI agent) to [Railway](https://railway.com), fronted by [Hermes WebUI](https://github.com/nesquena/hermes-webui) — the popular community web interface for Hermes — with an optional private SearXNG search service.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent-all-in-one)

Two-service Railway deployment:

- **Hermes Agent** — Hermes Agent itself plus Hermes WebUI on the public HTTP port. Optional in-container messaging gateway daemon (Telegram/Discord/Slack/email).
- **SearXNG** — private metasearch consumed by Hermes through `SEARXNG_URL`.

## What you get

A single password-protected web UI on the Railway public domain ([screenshots](https://github.com/nesquena/hermes-webui)):

- **Three-panel layout**: sessions sidebar · chat · workspace browser
- **First-run onboarding wizard** — pick a provider, paste an API key, you're ready
- **Hermes Control Center** with cron, skills, memory, profiles, settings, todo
- **Model picker, profile switcher, configurable model badges** in the composer footer
- **Session search**, slash-command autocomplete, streaming SSE responses, multi-modal uploads
- **Persistent state** on the `/data` volume — config, sessions, skills, workspace, WebUI state
- **Bundled `searxng-local` skill** so Hermes can query the companion SearXNG service
- **Official Hermes CLI dashboard** reachable at **`/hermes-dashboard`** on your Railway URL while `hermes dashboard --no-open` runs on loopback inside the container (Hermes SPA expects **`X-Forwarded-Prefix`**; see `admin/dashboard_proxy.py`).
- **Health check** at `/health` (Railway probe)
- **Web terminal (`/tui`)** — in-browser xterm with two modes: OAuth shortcut buttons that run `hermes auth add <provider> --type oauth` for ChatGPT (Codex) and Nous Portal device-code flows, plus a free-form `/bin/bash` pane for any other `hermes` CLI command. Useful when you want to use your ChatGPT subscription instead of paying for OpenAI API access, or when you need a shell without SSH access. New sessions automatically inherit the provider you configured (workaround for an upstream hermes-webui bug — see `admin/proxy.py:_active_provider`).

## Kanban & official dashboard vs Web UI

- **Kanban (upstream docs)** lives in the official **`hermes dashboard`** plugin (default bind **`127.0.0.1:9119`**, overridden with **`HERMES_DASHBOARD_HOST`** / **`HERMES_DASHBOARD_PORT`**). On Railway, open **`HERMES_DASHBOARD_MOUNT_PATH`** (default **`/hermes-dashboard`**) after **`hermes dashboard --no-open`** runs inside the container (e.g. from **`/tui`**). That is **not** the community **Hermes WebUI** at **`/`** ([Kanban tutorial](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban-tutorial), [Kanban reference](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban), [Web dashboard](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard)).
- **Board progression / dispatcher:** enable **`START_GATEWAY=true`** in this template (or run **`hermes gateway start`** alongside the dashboard as in the Hermes tutorials) so operators can move work forward from the board.
- **Embedded TUI in the dashboard:** if you set **`HERMES_DASHBOARD_TUI=1`**, you need Hermes’s **`pty`** extra in the install (e.g. `uv pip install -e ".[pty]"` under `/opt/hermes`, same `uv` pattern as the image’s `-e ".[all,messaging]"`); the template Dockerfile already pulls in **`ptyprocess`** for **`/tui`**, which is separate from that Hermes extra.

## Railway Services

### Service 1: Hermes Agent

Source this repository with root directory:

```text
hermes-agent-railway
```

Attach a Railway volume:

```text
/data
```

Set these variables:

| Variable | Value | Required | Description |
| --- | --- | --- | --- |
| `PORT` | `8080` | Yes | Public HTTP port. |
| `ADMIN_PASSWORD` | `${{ secret(32) }}` | Recommended | Password for Hermes WebUI (set explicitly in production). If unset, the entrypoint generates one, persists it to `/data/admin.password`, and prints it once to the deploy logs. |
| `SEARXNG_URL` | `http://${{searxng-railway.RAILWAY_PRIVATE_DOMAIN}}:${{searxng-railway.PORT}}` | Recommended | Private URL for the companion SearXNG service. |
| `START_GATEWAY` | `false` | Optional | Set to `true` to also run `hermes gateway run --replace` as a background daemon (messaging bridges; aligns with **`hermes gateway start`** in Hermes tutorials for Kanban/dispatcher workflows). Configure channel tokens in WebUI Settings first, then redeploy with this flag. |
| `HERMES_WEBUI_HOST` | `127.0.0.1` | Optional | Loopback bind address for Hermes WebUI — must reach the proxy target; Railway operators normally leave default. |
| `HERMES_WEBUI_PORT` | `9120` | Optional | Internal WebUI TCP port. Default frees **9119** for upstream **`hermes dashboard`**, often run manually from **`/tui`**; change only when both processes need different ports on your deployment. |
| `HERMES_DASHBOARD_HOST` | `127.0.0.1` | Optional | [**`hermes dashboard`**](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard) bind address (also **`--host`** in the CLI); default stays on container loopback. Railway does **not** expose a separate public port — use **`HERMES_DASHBOARD_MOUNT_PATH`** on the **`$PORT`** surface instead (`0.0.0.0` alone doesn’t magically map to HTTPS). Also described in [**environment variables**](https://hermes-agent.nousresearch.com/docs/reference/environment-variables). |
| `HERMES_DASHBOARD_PORT` | `9119` | Optional | Port for **`hermes dashboard`**; must match **`admin/dashboard_proxy.py`**. |
| `HERMES_DASHBOARD_MOUNT_PATH` | `/hermes-dashboard` | Optional | HTTPS path prefix proxied with **`X-Forwarded-Prefix`**. Avoid names that collide with Hermes WebUI (`/login`, `/health`, `/assets`, **`/tui`**, `/api`, `/ws`, `/settings`, `/chat`). |

You can also set provider keys as Railway variables (or configure them later through the WebUI):

| Variable | Description |
| --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter API key. Easiest first provider for free models. |
| `ANTHROPIC_API_KEY` | Anthropic API key. |
| `OPENAI_API_KEY` | OpenAI or custom OpenAI-compatible API key. |
| `GOOGLE_API_KEY` | Google AI Studio / Gemini API key. |
| `GLM_API_KEY` | z.ai / GLM API key. |
| `KIMI_API_KEY` | Kimi API key. |
| `MINIMAX_API_KEY` | MiniMax API key. |
| `HF_TOKEN` | Hugging Face token. |

Messaging channel variables (used when `START_GATEWAY=true`):

| Variable | Description |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather. |
| `TELEGRAM_ALLOWED_USERS` | Comma-separated Telegram user IDs, or `*`. |
| `DISCORD_BOT_TOKEN` | Discord bot token. |
| `DISCORD_ALLOWED_USERS` | Comma-separated Discord user IDs. |
| `SLACK_BOT_TOKEN` | Slack bot token. |
| `SLACK_APP_TOKEN` | Slack app token for socket mode. |
| `SLACK_ALLOWED_USERS` | Comma-separated Slack user IDs. |

### Telegram on Railway

Hermes defaults to **long polling** against Telegram — no webhook URL needed; once `START_GATEWAY=true` and your bot credentials are configured, the gateway pulls updates outbound.

Alternatively you can use **webhook** mode per upstream docs: set `TELEGRAM_WEBHOOK_URL` (public HTTPS endpoint Telegram can reach), `TELEGRAM_WEBHOOK_SECRET` (required when using webhooks; generate e.g. with `openssl rand -hex 32`), and optionally `TELEGRAM_WEBHOOK_PORT` for the **local** port the webhook server binds to (often not the same as Railway’s `$PORT`; your reverse proxy must route the public HTTPS path to that listener). Railway’s managed HTTPS terminates at your service `$PORT`; follow the Hermes webhook guidance so the externally advertised URL matches what Telegram posts to.

- [Hermes: Telegram messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)
- [Hermes: environment variables reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables)

### Service 2: SearXNG

Use the existing Protemplate SearXNG source/template from [SearXNG on Railway](https://railway.com/deploy/searxng-w-official-i).

Recommended variables:

| Variable | Value | Description |
| --- | --- | --- |
| `PORT` | `8080` | Required so Hermes can reference `${{searxng-railway.PORT}}`. |
| `SEARXNG_SECRET_KEY` | `${{secret()}}` | Session secret for SearXNG. |
| `SEARXNG_UWSGI_WORKERS` | `4` | Optional worker count. |
| `SEARXNG_UWSGI_THREADS` | `4` | Optional thread count. |

SearXNG can be private-only if Hermes is the only consumer. Keep public HTTP enabled if you want users to access the SearXNG web UI.

## First Run

1. Deploy the two-service template.
2. Open the Hermes Agent public Railway URL.
3. Enter the WebUI password (your `ADMIN_PASSWORD` if set; otherwise retrieve it once from `/data/admin.password` or deploy logs — see deploy variables table).
4. The WebUI's onboarding wizard launches — pick a provider, paste an API key, choose a default model. Configuration is written to `/data/.env` and `/data/config.yaml`.
5. Send your first message in the chat.
6. (Optional) To enable Telegram/Discord/Slack/email bridges, configure the channel tokens in **Settings**, then redeploy with `START_GATEWAY=true`.

## SearXNG Search

The Hermes container syncs `skills/searxng-local` into `/data/skills/searxng-local` at startup. Once Hermes is configured, it loads the skill from `/data/skills/`.

The skill instructs Hermes to query:

```text
${SEARXNG_URL}/search?q=YOUR_QUERY&format=json
```

Railway private networking requires HTTP and an explicit port:

```text
http://${{searxng-railway.RAILWAY_PRIVATE_DOMAIN}}:${{searxng-railway.PORT}}
```

## Local Development

Build locally:

```bash
docker build -t hermes-agent-railway ./hermes-agent-railway
```

Run with Docker:

```bash
docker run --rm -p 8080:8080 \
  -e PORT=8080 \
  -e ADMIN_PASSWORD=changeme \
  -e SEARXNG_URL=http://searxng:8080 \
  -v hermes-data:/data \
  hermes-agent-railway
```

Or run the local compose stack:

```bash
cd hermes-agent-railway
docker compose up --build
```

Open `http://localhost:8080` and enter `changeme` at the password prompt.

## Operations

- Web UI: `/`
- **Official Hermes CLI dashboard:** **`/hermes-dashboard`** (configure with **`HERMES_DASHBOARD_MOUNT_PATH`**) — after **`hermes dashboard --no-open`** runs on **`127.0.0.1:9119`** (usually from **`/tui`**). If the process isn’t listening, open this path and you’ll get **502** with hints.
- Web terminal (OAuth shortcuts + shell): `/tui`
- Health check: `/health`
- Gateway log (when `START_GATEWAY=true`): `/data/logs/gateway.log`
- Gateway PID file: `/data/gateway.pid`
- WebUI internal log: `/data/logs/webui.log`
- WebUI state: `/data/.hermes/webui/`
- Hermes config: `/data/config.yaml`
- Hermes env: `/data/.env`

### How `/tui` works

`/tui` is a two-pane web terminal: a left rail of preset buttons (OAuth login, status commands, file viewers, "Open shell") and a right pane with xterm.js. Behind it are two WebSocket modes:

- **OAuth one-shots** (`/tui/ws/auth/<provider>`) — clicking an OAuth button spawns a dedicated PTY running `hermes auth add <provider> --type oauth --no-browser`. On clean exit, the wrapper writes `model.provider` into `/data/config.yaml`, marks onboarding complete, restarts hermes-webui, and the page redirects you to `/`.
- **Shell** (`/tui/ws/shell`) — clicking "Open shell" or any non-OAuth preset spawns `/bin/bash -i` with `cwd=/data` and Hermes' venv on PATH. Run `hermes status`, `hermes auth list`, `cat /data/config.yaml`, `tail .../webui.log` — anything you'd run over SSH.

### `hermes update` from the Web Terminal

Hermes installs live under `/opt/hermes`. Git 2.35+ refuses pulls when the checkout is owned by a different Unix user than the one running Git (the classic error: **"detected dubious ownership"**). Older template images cloned that tree as root but ran shells as user `hermes`, so `hermes update` failed. Current images **`chown` `/opt/hermes` and `/opt/hermes-webui` to `hermes`** and set **`safe.directory`** in the system Git config so in-container updates work reliably. Redeploy from this template to pick up the fix.

The **`/tui` shortcut OAuth buttons** are only wired for ChatGPT (**Codex**) and **Nous Portal**. Other OAuth-capable providers are reached interactively (`hermes model`, `hermes auth add …`, etc.) via the **`/bin/bash` shell pane** — same device-code flow where applicable — not extra one-click presets. ChatGPT/Codex and Nous Portal use the **OAuth device-code grant (RFC 8628)**, which doesn't require a localhost callback — workable for remote deployments. The Codex/Nous shortcut flow:


1. Visit `/tui` (you must already be logged in to the Hermes WebUI).
2. Click **Login with ChatGPT (Codex)** or **Login with Nous Portal**.
3. The in-browser terminal prints a verification URL plus a short code.
4. Open the URL in another tab on any device, sign in to the provider, and enter the code.
5. The CLI completes the flow, writes credentials to `/data/auth.json`, and exits with `Added <provider> OAuth credential #1`.
6. The page redirects to `/` once the WebUI is back up — your provider is now configured.

API-key providers (OpenRouter, OpenAI, Anthropic, Google Gemini, etc.) use the in-wizard form on the WebUI's onboarding page — no need to visit `/tui`.

### Official **`hermes dashboard`** on Railway

The CLI uses **`127.0.0.1:9119`** by default (flags **`--host`**, **`--port`**, **`--no-open`**; see [**Web Dashboard**](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard)). That address is **only inside the container** — you cannot browse it directly from your laptop via Railway.

Start it from **`/tui`** (for example **`hermes dashboard --no-open`**) with **[`HERMES_DASHBOARD_HOST`](https://hermes-agent.nousresearch.com/docs/reference/environment-variables)** / **`HERMES_DASHBOARD_PORT`** if you relocate the listener, then visit:

```text
https://<railway-domain>/hermes-dashboard
```

The wrapper sets **`X-Forwarded-Prefix: /hermes-dashboard`** when forwarding to **`HERMES_DASHBOARD_*`**, matching how Hermes rewrites SPA assets behind a prefix (`hermes_cli/web_server.py`).

**Kanban:** use this **`https://<railway-domain>/hermes-dashboard`** path (see [Kanban & official dashboard vs Web UI](#kanban--official-dashboard-vs-web-ui) above), not the WebUI root **`/`**.

**Important:** **`0.0.0.0` binding does not give you another published Railway listener** inside this single-service HTTP model — the **`$PORT`** app plus the **`/hermes-dashboard`** proxy is how you expose the CLI dashboard over HTTPS.

**Safety note:** **`/hermes-dashboard` bypasses Hermes WebUI `ADMIN_PASSWORD`.** Whenever **`hermes dashboard`** is listening, anyone who reaches your Railway URL gets Hermes's powerful CLI dashboard ([upstream exposes secrets](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard)). Prefer stopping **`hermes dashboard`** when you do not need it.

## Notes

- **Ports:** The public Railway listener stays **`PORT`**. Hermes WebUI listens on **`HERMES_WEBUI_HOST`/`HERMES_WEBUI_PORT`** (default **127.0.0.1:9120**) and is reverse-proxied from **`/`**. The **`hermes dashboard`** subprocess defaults to **`127.0.0.1:9119`** (see **`HERMES_DASHBOARD_*`** defaults in [`entrypoint.sh`](./entrypoint.sh)); traffic is surfaced at **`HERMES_DASHBOARD_MOUNT_PATH`**, not via a separate Railway port.
- **Skills at boot:** Upstream Docker runs `tools/skills_sync.py` from the Hermes install tree to mirror bundled skills onto the volume. This template does **not** run that script; only bundles under [`hermes-agent-railway/skills/`](./skills/) are copied into `/data/skills` each start. Operators who expect **all** upstream stock skills mirrored should sync them manually or adjust the deployment.
- Hermes Agent is installed from upstream `NousResearch/hermes-agent` (`ARG HERMES_REF=v2026.6.5`). Override with any valid branch, tag, or SHA.
- Hermes WebUI is pinned to a specific tag (`ARG HERMES_WEBUI_REF=v0.51.310`). Override to upgrade.
- `/data` stores config, `.env`, sessions, memories, skills, workspace files, logs, and WebUI state.
