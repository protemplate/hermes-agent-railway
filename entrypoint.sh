#!/bin/bash
set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-/data}"
export PORT="${PORT:-8080}"
export PATH="/opt/hermes/.venv/bin:/data/.local/bin:${PATH}"
export PYTHONPATH="/opt/hermes-railway:/opt/hermes-webui:/opt/hermes:${PYTHONPATH:-}"

ADMIN_PASSWORD_FILE="${HERMES_HOME}/admin.password"

mkdir -p \
  "${HERMES_HOME}/.hermes" \
  "${HERMES_HOME}/.hermes/webui" \
  "${HERMES_HOME}/cron" \
  "${HERMES_HOME}/home" \
  "${HERMES_HOME}/hooks" \
  "${HERMES_HOME}/logs" \
  "${HERMES_HOME}/memories" \
  "${HERMES_HOME}/plans" \
  "${HERMES_HOME}/sessions" \
  "${HERMES_HOME}/skills" \
  "${HERMES_HOME}/skins" \
  "${HERMES_HOME}/workspace"

if [ "$(id -u)" = "0" ]; then
  chown -R hermes:hermes "${HERMES_HOME}" 2>/dev/null || true
fi

# Do NOT pre-seed .env or config.yaml — hermes-webui's first-run wizard creates
# them when the user picks a provider and enters an API key. Pre-seeding from
# /opt/hermes/cli-config.yaml.example sets model.default and makes the WebUI
# think provider_configured=true, which skips the wizard entirely.

# One-shot rescue for users upgrading from earlier image versions that pre-seeded
# config.yaml: that pattern caused hermes-webui to persist onboarding_completed=true
# in /data/.hermes/webui/settings.json. Without resetting it, the wizard stays
# hidden even after we drop the seeding. Run only when no real provider key is
# present in /data/.env.
WEBUI_SETTINGS="${HERMES_HOME}/.hermes/webui/settings.json"
if [ -f "${WEBUI_SETTINGS}" ] && [ ! -f "${HERMES_HOME}/.env" ]; then
  python3 - <<PY || true
import json, pathlib
p = pathlib.Path("${WEBUI_SETTINGS}")
try:
    s = json.loads(p.read_text())
except Exception:
    raise SystemExit(0)
if s.get("onboarding_completed"):
    s["onboarding_completed"] = False
    p.write_text(json.dumps(s, indent=2))
    print("[entrypoint] reset onboarding_completed (no .env present)")
PY
fi

if [ ! -f "${HERMES_HOME}/SOUL.md" ] && [ -f "/opt/hermes/docker/SOUL.md" ]; then
  cp /opt/hermes/docker/SOUL.md "${HERMES_HOME}/SOUL.md"
fi

# Self-heal: when auth.json has an OAuth credential but config.yaml lacks
# model.provider, the agent fails to determine credentials. The Hermes CLI's
# `hermes model` picker doesn't always set model.provider when an OAuth-already-
# authenticated provider is selected; this normalizes the state at boot so
# users don't hit "No LLM provider configured" after running the picker.
if [ -f "${HERMES_HOME}/auth.json" ] && [ -f "${HERMES_HOME}/config.yaml" ]; then
  python3 - <<PY || true
import json, sys, pathlib
import yaml
auth_p = pathlib.Path("${HERMES_HOME}/auth.json")
cfg_p = pathlib.Path("${HERMES_HOME}/config.yaml")

# Map of provider -> base_url for OAuth providers we can self-heal.
PROVIDER_BASE = {
    "openai-codex": "https://chatgpt.com/backend-api/codex",
    "nous": "https://inference-api.nousresearch.com/v1",
}

try:
    auth = json.loads(auth_p.read_text())
    cfg = yaml.safe_load(cfg_p.read_text()) or {}
except Exception:
    sys.exit(0)

providers = (auth or {}).get("providers", {})
authed = [p for p in providers.keys() if p in PROVIDER_BASE]
if not authed:
    sys.exit(0)

model_cfg = cfg.get("model")
if not isinstance(model_cfg, dict):
    sys.exit(0)
if model_cfg.get("provider"):
    sys.exit(0)

# Pick the first OAuth provider we recognize.
chosen = authed[0]
model_cfg["provider"] = chosen
if not model_cfg.get("base_url"):
    model_cfg["base_url"] = PROVIDER_BASE[chosen]
cfg["model"] = model_cfg
yaml.safe_dump(cfg, cfg_p.open("w"), sort_keys=False)
print(f"[entrypoint] healed config.yaml: model.provider={chosen}")
PY
fi

if [ -d "/opt/hermes-railway/skills" ]; then
  for skill_dir in /opt/hermes-railway/skills/*/; do
    [ -d "${skill_dir}" ] || continue
    skill_name="$(basename "${skill_dir}")"
    rm -rf "${HERMES_HOME}/skills/${skill_name}"
    cp -R "${skill_dir}" "${HERMES_HOME}/skills/${skill_name}"
    echo "Synced Railway skill: ${skill_name}"
  done
fi

if [ -z "${ADMIN_PASSWORD:-}" ]; then
  if [ -f "${ADMIN_PASSWORD_FILE}" ]; then
    ADMIN_PASSWORD="$(tr -d '\n' < "${ADMIN_PASSWORD_FILE}")"
  else
    ADMIN_PASSWORD="$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
    printf '%s\n' "${ADMIN_PASSWORD}" > "${ADMIN_PASSWORD_FILE}"
    chmod 600 "${ADMIN_PASSWORD_FILE}" 2>/dev/null || true
  fi
  echo ""
  echo "Hermes WebUI password was generated automatically."
  echo "Password: ${ADMIN_PASSWORD}"
  echo ""
fi
export ADMIN_PASSWORD

# hermes-webui runs internally on 127.0.0.1:9119; our wrapper proxies $PORT -> 9119.
export HERMES_WEBUI_PASSWORD="${ADMIN_PASSWORD}"
export HERMES_WEBUI_HOST="127.0.0.1"
export HERMES_WEBUI_PORT="9119"
export HERMES_WEBUI_STATE_DIR="${HERMES_HOME}/.hermes/webui"
# Point hermes-webui at our Hermes Agent install so agent features work
export HERMES_WEBUI_AGENT_DIR="/opt/hermes"

# Optional: auto-start the messaging gateway daemon (Telegram/Discord/Slack/email).
# Default off — users typically configure channel tokens via the WebUI Settings
# panel first, then redeploy with START_GATEWAY=true.
if [ "${START_GATEWAY:-false}" = "true" ]; then
  GATEWAY_LOG="${HERMES_HOME}/logs/gateway.log"
  GATEWAY_PID_FILE="${HERMES_HOME}/gateway.pid"
  echo "Starting Hermes messaging gateway in background..."
  printf '\n--- Starting Hermes gateway %s ---\n' "$(date)" >> "${GATEWAY_LOG}"
  if [ "$(id -u)" = "0" ]; then
    setsid gosu hermes /opt/hermes/.venv/bin/hermes gateway run --replace \
      >> "${GATEWAY_LOG}" 2>&1 < /dev/null &
  else
    setsid /opt/hermes/.venv/bin/hermes gateway run --replace \
      >> "${GATEWAY_LOG}" 2>&1 < /dev/null &
  fi
  echo $! > "${GATEWAY_PID_FILE}"
  echo "Gateway PID: $(cat "${GATEWAY_PID_FILE}") (logs at ${GATEWAY_LOG})"
fi

WEBUI_LOG="${HERMES_HOME}/logs/webui.log"

if [ "$(id -u)" = "0" ]; then
  chown -R hermes:hermes "${HERMES_HOME}" /opt/hermes-railway 2>/dev/null || true
  printf '\n--- Starting Hermes WebUI on 127.0.0.1:9119 %s ---\n' "$(date)" >> "${WEBUI_LOG}"
  setsid gosu hermes python3 /opt/hermes-webui/server.py >> "${WEBUI_LOG}" 2>&1 < /dev/null &
  echo "Hermes WebUI PID: $!  (logs at ${WEBUI_LOG})"
  exec gosu hermes python3 -m uvicorn admin.app:app --host 0.0.0.0 --port "${PORT}"
fi

printf '\n--- Starting Hermes WebUI on 127.0.0.1:9119 %s ---\n' "$(date)" >> "${WEBUI_LOG}"
setsid python3 /opt/hermes-webui/server.py >> "${WEBUI_LOG}" 2>&1 < /dev/null &
echo "Hermes WebUI PID: $!  (logs at ${WEBUI_LOG})"
exec python3 -m uvicorn admin.app:app --host 0.0.0.0 --port "${PORT}"
