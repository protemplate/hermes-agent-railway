# Deploy and Host Hermes Agent with SearXNG on Railway

Hermes Agent is a self-improving, open-source AI agent from Nous Research. It connects to messaging channels such as Telegram, Discord, Slack, WhatsApp, Signal, and Email, remembers previous work, creates and improves skills, and can run scheduled automations. This Railway template packages Hermes with a small web admin dashboard for first-run setup and day-to-day gateway control.

The template also includes a companion SearXNG service so Hermes has private, self-hosted web search available without third-party search API keys.

## About Hosting Hermes Agent

Hosting Hermes Agent on Railway runs a containerized Hermes gateway and admin dashboard. The admin dashboard writes Hermes configuration into the persistent `/data` volume, starts and stops `hermes gateway run --replace`, and streams gateway logs. Users can configure an LLM provider, model, SearXNG URL, and messaging channel credentials from the browser.

All long-lived Hermes data is stored on the Railway volume at `/data`, including `.env`, `config.yaml`, conversations, memory, skills, workspace files, and logs.

## Common Use Cases

- **Personal AI assistant**: Run a private Telegram or Discord agent that remembers context and uses tools.
- **Team assistant**: Connect Hermes to Slack or Discord for team workflows with shared memory.
- **Research agent**: Use the bundled SearXNG service for web search and source discovery.
- **Scheduled automations**: Use Hermes cron capabilities for recurring reports, checks, and reminders.

## Dependencies for Hermes Agent Hosting

- **Hermes Agent**: Installed from `https://github.com/NousResearch/hermes-agent`.
- **SearXNG**: Optional but recommended companion search service.
- **Railway volume**: Mount at `/data` for persistent state.
- **LLM provider**: OpenRouter, Anthropic, OpenAI, Gemini, z.ai, Kimi, MiniMax, Hugging Face, or a custom OpenAI-compatible endpoint.
- **Messaging credentials**: At least one messaging channel token, such as Telegram or Discord.

### Deployment Dependencies

- Runtime: Python 3.13, Node.js, uv
- Admin server: Starlette + Uvicorn
- Browser tooling: Playwright Chromium from upstream Hermes image build
- Health check: `/health`
- Volume: `/data`

### Implementation Details

The Hermes service runs one public web server that unifies several surfaces behind a single password-protected dashboard:

```text
User -> Hermes Admin ($PORT)
        |-> /              (auto-redirect: /onboard on first run, /lite afterwards)
        |-> /lite          Lite Panel — status + gateway/web readiness + controls
        |-> /onboard       Web Wizard — xterm.js -> WebSocket -> PTY -> hermes setup
        |-> /tui           Web TUI    — xterm.js -> WebSocket -> PTY -> hermes (chat)
        |-> /hermes/*      Reverse proxy to native Hermes web on 127.0.0.1:9119
        |-> /setup         Power-user form for editing env vars and config.yaml
        |-> /logs          Tail of the gateway log
        `-> /health        JSON probe (gatewayRunning, webDashboardReady, ...)
```

The native Hermes web server (`python -m hermes_cli.main web --no-open --port 9119`) is started automatically by the Starlette lifespan and respawned with backoff on exit. The gateway (`hermes gateway run --replace`) is started/stopped from the Lite Panel.

The SearXNG service is referenced from Hermes with Railway private networking:

```text
SEARXNG_URL=http://${{SearXNG.RAILWAY_PRIVATE_DOMAIN}}:${{SearXNG.PORT}}
```

Set `PORT=8080` on SearXNG so the reference can resolve. Railway's runtime-provided `PORT` is not available to template variable references unless it is also set as a service variable.

## Template Variables

Hermes Agent:

| Variable | Default | Required |
| --- | --- | --- |
| `PORT` | `8080` | Yes |
| `ADMIN_USERNAME` | `admin` | Yes |
| `ADMIN_PASSWORD` | `${{secret()}}` | Yes |
| `SEARXNG_URL` | `http://${{SearXNG.RAILWAY_PRIVATE_DOMAIN}}:${{SearXNG.PORT}}` | Recommended |

SearXNG:

| Variable | Default | Required |
| --- | --- | --- |
| `PORT` | `8080` | Yes |
| `SEARXNG_SECRET_KEY` | `${{secret()}}` | Yes |
| `SEARXNG_UWSGI_WORKERS` | `4` | No |
| `SEARXNG_UWSGI_THREADS` | `4` | No |

## Why Deploy Hermes Agent with SearXNG on Railway?

Railway handles HTTPS, builds, deployment, logs, volumes, and private networking. This template keeps Hermes and search in one Railway project while avoiding public network traffic between services. Users can deploy, configure providers and channels from the browser, and keep agent state on a persistent Railway volume.
