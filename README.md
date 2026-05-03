# Hermes Agent Railway Template

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent), the self-improving AI agent by Nous Research, to [Railway](https://railway.com) with a web admin dashboard and an optional private SearXNG search service.

This template is designed for a two-service Railway deployment:

- `Hermes Agent` runs the admin dashboard and Hermes messaging gateway.
- `SearXNG` provides private metasearch for Hermes through `SEARXNG_URL`.

## Features

- Password-protected admin dashboard on Railway's public HTTP domain
- Setup form for model provider keys, default model, SearXNG URL, and messaging channels
- Gateway controls for start, stop, restart, and log viewing
- Persistent Hermes state at `/data`
- Bundled `searxng-local` skill that teaches Hermes how to call SearXNG's JSON API
- Health check at `/health`

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
| `PORT` | `8080` | Yes | Explicit port for public and private networking references. |
| `ADMIN_USERNAME` | `admin` | Yes | Admin dashboard username. |
| `ADMIN_PASSWORD` | `${{secret()}}` | Yes | Admin dashboard password. |
| `SEARXNG_URL` | `http://${{SearXNG.RAILWAY_PRIVATE_DOMAIN}}:${{SearXNG.PORT}}` | Recommended | Private URL for the companion SearXNG service. |

You can also configure provider keys through the dashboard after deployment, or set them as Railway variables:

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

Messaging channel variables can also be set in the dashboard:

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
| `PORT` | `8080` | Required so Hermes can reference `${{SearXNG.PORT}}`. |
| `SEARXNG_SECRET_KEY` | `${{secret()}}` | Session secret for SearXNG. |
| `SEARXNG_UWSGI_WORKERS` | `4` | Optional worker count. |
| `SEARXNG_UWSGI_THREADS` | `4` | Optional thread count. |

SearXNG can be private-only if Hermes is the only consumer. Keep public HTTP enabled if you want users to access the SearXNG web UI.

## First Run

1. Deploy the two-service template.
2. Open the Hermes Agent public Railway URL.
3. Log in with `ADMIN_USERNAME` and `ADMIN_PASSWORD`.
4. Open **Setup** and choose your provider, model, and channel credentials.
5. Save configuration.
6. Start the gateway from the dashboard.
7. Message your configured channel, such as Telegram or Discord.

## SearXNG Search

The Hermes container syncs `skills/searxng-local` into `/data/skills/searxng-local` at startup. The admin setup also ensures Hermes loads `/data/skills` through `skills.load.extraDirs`.

The skill instructs Hermes to query:

```text
${SEARXNG_URL}/search?q=YOUR_QUERY&format=json
```

Railway private networking requires HTTP and an explicit port:

```text
http://${{SearXNG.RAILWAY_PRIVATE_DOMAIN}}:${{SearXNG.PORT}}
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
  -e ADMIN_USERNAME=admin \
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

Open `http://localhost:8080` and log in with `admin` / `changeme`.

## Operations

- Admin dashboard: `/`
- Setup: `/setup`
- Logs: `/logs`
- Health check: `/health`
- Gateway log file: `/data/logs/gateway.log`
- Gateway PID file: `/data/gateway.pid`

## Notes

- Hermes Agent is installed from upstream `NousResearch/hermes-agent` with `ARG HERMES_REF=main`. Override the build arg with any valid branch, tag, or SHA when you want to pin a release.
- `/data` stores config, `.env`, sessions, memories, skills, workspace files, and logs.
- If `ADMIN_PASSWORD` is not set, the entrypoint creates a persistent random password at `/data/admin.password` and prints it to deployment logs.
