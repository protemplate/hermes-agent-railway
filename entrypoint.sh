#!/bin/bash
set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-/data}"
export PORT="${PORT:-8080}"
export PATH="/opt/hermes/.venv/bin:/data/.local/bin:${PATH}"
export PYTHONPATH="/opt/hermes-webui:/opt/hermes:${PYTHONPATH:-}"

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

if [ ! -f "${HERMES_HOME}/.env" ] && [ -f "/opt/hermes/.env.example" ]; then
  cp /opt/hermes/.env.example "${HERMES_HOME}/.env"
fi

if [ ! -f "${HERMES_HOME}/config.yaml" ] && [ -f "/opt/hermes/cli-config.yaml.example" ]; then
  cp /opt/hermes/cli-config.yaml.example "${HERMES_HOME}/config.yaml"
fi

if [ ! -f "${HERMES_HOME}/SOUL.md" ] && [ -f "/opt/hermes/docker/SOUL.md" ]; then
  cp /opt/hermes/docker/SOUL.md "${HERMES_HOME}/SOUL.md"
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

# hermes-webui reads HERMES_WEBUI_PASSWORD for optional auth
export HERMES_WEBUI_PASSWORD="${ADMIN_PASSWORD}"
export HERMES_WEBUI_HOST="0.0.0.0"
export HERMES_WEBUI_PORT="${PORT}"
export HERMES_WEBUI_STATE_DIR="${HERMES_HOME}/.hermes/webui"

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

if [ "$(id -u)" = "0" ]; then
  chown -R hermes:hermes "${HERMES_HOME}" /opt/hermes-railway 2>/dev/null || true
  exec gosu hermes python3 /opt/hermes-webui/server.py
fi

exec python3 /opt/hermes-webui/server.py
