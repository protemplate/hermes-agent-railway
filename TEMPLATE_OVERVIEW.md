# Deploy and Host Hermes Agent with SearXNG on Railway

Hermes Agent is a self-improving, open-source AI agent from Nous Research. It connects to messaging channels such as Telegram, Discord, Slack, WhatsApp, Signal, and Email, remembers previous work, creates and improves skills, and can run scheduled automations. This Railway template packages Hermes with [Hermes WebUI](https://github.com/nesquena/hermes-webui) — the popular community web interface — so you get a polished browser experience for chat, sessions, settings, and onboarding right after deploy.

The template also includes a companion SearXNG service so Hermes has private, self-hosted web search available without third-party search API keys.

## About Hosting Hermes Agent

Hosting Hermes Agent on Railway runs a containerized Hermes Agent installation with Hermes WebUI on the public HTTP port. The WebUI persists configuration into the `/data` volume (`.env`, `config.yaml`, sessions, skills, memory, workspace files, WebUI state). Users configure an LLM provider, default model, channel credentials, and skills directly through the WebUI's Hermes Control Center.

An optional `START_GATEWAY=true` environment variable enables the messaging gateway daemon (`hermes gateway run --replace`) so Telegram/Discord/Slack/email bridges run alongside the WebUI in the same container.

## Common Use Cases

- **Personal AI assistant**: Chat with Hermes from any browser; persistent memory across sessions.
- **Team assistant**: Connect Hermes to Slack/Discord for team workflows with shared memory.
- **Research agent**: Use the bundled SearXNG service for web search and source discovery.
- **Scheduled automations**: Hermes cron capabilities for recurring reports, checks, reminders.

## Dependencies for Hermes Agent Hosting

- **Hermes Agent**: Installed from `https://github.com/NousResearch/hermes-agent` (`HERMES_REF=main` by default).
- **Hermes WebUI**: Installed from `https://github.com/nesquena/hermes-webui` (pinned via `HERMES_WEBUI_REF=v0.50.278`).
- **SearXNG**: Companion search service.
- **Railway volume**: Mount at `/data` for persistent state.
- **LLM provider**: OpenRouter, Anthropic, OpenAI, Gemini, z.ai, Kimi, MiniMax, Hugging Face, or a custom OpenAI-compatible endpoint.

### Deployment Dependencies

- Runtime: Python 3.13, Node.js, uv
- Web UI: Hermes WebUI (Python stdlib + vanilla JS, no build step)
- Browser tooling: Playwright Chromium (agent-side, used when Hermes invokes the browser tool)
- Health check: `/health`
- Volume: `/data`

### Implementation Details

The Hermes service runs a thin Starlette wrapper on `$PORT`. The wrapper serves `/auth-cli` (an in-browser xterm for `hermes login --provider <X>` device-code flows) and reverse-proxies everything else to Hermes WebUI on the loopback:

```text
User -> Wrapper ($PORT) ──┬── /auth-cli            xterm running `hermes login`
                          └── /*                   reverse-proxy → 127.0.0.1:9119
                                                                   (Hermes WebUI)

Hermes WebUI (127.0.0.1:9119)
        |-> /              Three-panel UI (sessions / chat / workspace)
        |-> /health        Health probe (Railway uses this through the proxy)
        `-> /api/*         Chat, sessions, settings, cron, skills, profiles, memory APIs

Background (optional, when START_GATEWAY=true):
        hermes gateway run --replace   (Telegram/Discord/Slack/email bridges)
```

Both Hermes Agent and Hermes WebUI run inside the same container and share the `/data` volume, so the WebUI reads/writes the same `~/.hermes/` state the Agent uses. Auth on `/auth-cli` reuses the WebUI's `hermes_session` cookie, so users don't have to log in twice.

The SearXNG service is referenced from Hermes with Railway private networking:

```text
SEARXNG_URL=http://${{searxng-railway.RAILWAY_PRIVATE_DOMAIN}}:${{searxng-railway.PORT}}
```

Set `PORT=8080` on SearXNG so the reference can resolve. Railway's runtime-provided `PORT` is not available to template variable references unless it is also set as a service variable.

## Template Variables

Hermes Agent:

| Variable | Default | Required |
| --- | --- | --- |
| `PORT` | `8080` | Yes |
| `ADMIN_PASSWORD` | `${{ secret(32) }}` | Yes |
| `SEARXNG_URL` | `http://${{searxng-railway.RAILWAY_PRIVATE_DOMAIN}}:${{searxng-railway.PORT}}` | Recommended |
| `START_GATEWAY` | `false` | No |

SearXNG:

| Variable | Default | Required |
| --- | --- | --- |
| `PORT` | `8080` | Yes |
| `SEARXNG_SECRET_KEY` | `${{secret()}}` | Yes |
| `SEARXNG_UWSGI_WORKERS` | `4` | No |
| `SEARXNG_UWSGI_THREADS` | `4` | No |

## Why Deploy Hermes Agent with SearXNG on Railway?

Railway handles HTTPS, builds, deployment, logs, volumes, and private networking. This template keeps Hermes and search in one Railway project while avoiding public network traffic between services. The Hermes WebUI integration means users get a production-quality interface from the moment the deploy finishes — no terminal access required, no hand-built admin panel — and configuration persists on the `/data` volume across redeploys.
