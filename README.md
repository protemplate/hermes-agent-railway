# Hermes Agent Railway Template

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) (NousResearch's self-improving AI agent) to [Railway](https://railway.com), fronted by [Hermes WebUI](https://github.com/nesquena/hermes-webui) — the popular community web interface for Hermes — with an optional private SearXNG search service.

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
- **Health check** at `/health` (Railway probe)

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
| `ADMIN_PASSWORD` | `${{ secret(32) }}` | Yes | Password for Hermes WebUI. Materialized once at template deploy. |
| `SEARXNG_URL` | `http://${{searxng-railway.RAILWAY_PRIVATE_DOMAIN}}:${{searxng-railway.PORT}}` | Recommended | Private URL for the companion SearXNG service. |
| `START_GATEWAY` | `false` | Optional | Set to `true` to also run `hermes gateway run --replace` as a background daemon (messaging bridges). Configure channel tokens in WebUI Settings first, then redeploy with this flag. |

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
3. Enter `ADMIN_PASSWORD` when prompted.
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
- Health check: `/health`
- Gateway log (when `START_GATEWAY=true`): `/data/logs/gateway.log`
- Gateway PID file: `/data/gateway.pid`
- WebUI state: `/data/.hermes/webui/`
- Hermes config: `/data/config.yaml`
- Hermes env: `/data/.env`

## Notes

- Hermes Agent is installed from upstream `NousResearch/hermes-agent` (`ARG HERMES_REF=main`). Override with any valid branch, tag, or SHA.
- Hermes WebUI is pinned to a specific tag (`ARG HERMES_WEBUI_REF=v0.50.278`). Override to upgrade.
- `/data` stores config, `.env`, sessions, memories, skills, workspace files, logs, and WebUI state.
- If `ADMIN_PASSWORD` is empty, the entrypoint generates one and persists it at `/data/admin.password`, also printing it once to the deploy logs.
