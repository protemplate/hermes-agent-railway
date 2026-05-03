#!/bin/bash
set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-/data}"
export PORT="${PORT:-8080}"
export PATH="/opt/hermes/.venv/bin:/data/.local/bin:${PATH}"
export PYTHONPATH="/opt/hermes-railway:/opt/hermes:${PYTHONPATH:-}"

ADMIN_PASSWORD_FILE="${HERMES_HOME}/admin.password"

mkdir -p \
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
  export ADMIN_PASSWORD
  echo ""
  echo "Hermes Agent admin password was generated automatically."
  echo "Username: ${ADMIN_USERNAME:-admin}"
  echo "Password: ${ADMIN_PASSWORD}"
  echo ""
fi

if [ "$(id -u)" = "0" ]; then
  chown -R hermes:hermes "${HERMES_HOME}" /opt/hermes-railway 2>/dev/null || true
  exec gosu hermes python -m uvicorn admin.app:app --host 0.0.0.0 --port "${PORT}"
fi

exec python -m uvicorn admin.app:app --host 0.0.0.0 --port "${PORT}"
