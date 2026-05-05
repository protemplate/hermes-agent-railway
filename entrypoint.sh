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
# hidden even after we drop the seeding. Skip the reset when the user has a
# legitimate setup — either an .env (API-key providers) OR an OAuth credential
# in auth.json paired with model.provider in config.yaml (Codex/Nous flows).
WEBUI_SETTINGS="${HERMES_HOME}/.hermes/webui/settings.json"
if [ -f "${WEBUI_SETTINGS}" ] && [ ! -f "${HERMES_HOME}/.env" ]; then
  python3 - <<PY || true
import json, pathlib
import yaml

p = pathlib.Path("${WEBUI_SETTINGS}")
try:
    s = json.loads(p.read_text())
except Exception:
    raise SystemExit(0)
if not s.get("onboarding_completed"):
    raise SystemExit(0)

# OAuth-configured? Skip the reset.
auth_p = pathlib.Path("${HERMES_HOME}/auth.json")
cfg_p = pathlib.Path("${HERMES_HOME}/config.yaml")
oauth_ok = False
try:
    if auth_p.exists() and cfg_p.exists():
        auth = json.loads(auth_p.read_text()) or {}
        providers = (auth.get("providers") or {}) if isinstance(auth.get("providers"), dict) else {}
        cfg = yaml.safe_load(cfg_p.read_text()) or {}
        mc = cfg.get("model") or {}
        prov = (mc.get("provider") or "").strip()
        if prov and prov in providers:
            oauth_ok = True
except Exception:
    pass

if oauth_ok:
    print("[entrypoint] keeping onboarding_completed=true (OAuth provider configured)")
    raise SystemExit(0)

s["onboarding_completed"] = False
p.write_text(json.dumps(s, indent=2))
print("[entrypoint] reset onboarding_completed (no .env, no OAuth)")
PY
fi

if [ ! -f "${HERMES_HOME}/SOUL.md" ] && [ -f "/opt/hermes/docker/SOUL.md" ]; then
  cp /opt/hermes/docker/SOUL.md "${HERMES_HOME}/SOUL.md"
fi

# Self-heal: hermes-webui caches its model catalog at this path for 24 hours.
# If config.yaml changed externally (e.g. user ran `hermes model` after first
# boot), the cache stays stale and /api/models returns active_provider=None
# until the cache TTL expires. Wipe at boot so the catalog rebuilds fresh
# against the current config.yaml + auth.json.
rm -f "${HERMES_HOME}/.hermes/webui/models_cache.json" 2>/dev/null || true

# Self-heal: when auth.json has an OAuth credential but config.yaml lacks
# model.provider (or is missing entirely), the agent fails to determine
# credentials. The Hermes CLI's `hermes model` picker doesn't always set
# model.provider when an OAuth-already-authenticated provider is selected,
# AND `hermes auth add <provider>` doesn't write config.yaml at all.
# This normalizes the state at boot so users don't hit
# "No LLM provider configured" after running either flow.
if [ -f "${HERMES_HOME}/auth.json" ]; then
  python3 - <<PY || true
import json, sys, pathlib
import yaml
auth_p = pathlib.Path("${HERMES_HOME}/auth.json")
cfg_p = pathlib.Path("${HERMES_HOME}/config.yaml")

PROVIDER_BASE = {
    "openai-codex": "https://chatgpt.com/backend-api/codex",
    "nous": "https://inference-api.nousresearch.com/v1",
}
PROVIDER_DEFAULT_MODEL = {
    "openai-codex": "gpt-5.5",
    "nous": "Hermes-3-Llama-3.1-70B-FP8",
}

try:
    auth = json.loads(auth_p.read_text())
except Exception:
    sys.exit(0)

providers = (auth or {}).get("providers", {})
authed = [p for p in providers.keys() if p in PROVIDER_BASE]
if not authed:
    sys.exit(0)

if cfg_p.exists():
    try:
        cfg = yaml.safe_load(cfg_p.read_text()) or {}
    except Exception:
        cfg = {}
else:
    cfg = {}

model_cfg = cfg.get("model")
if not isinstance(model_cfg, dict):
    model_cfg = {}
if model_cfg.get("provider"):
    sys.exit(0)

chosen = authed[0]
model_cfg["provider"] = chosen
if not model_cfg.get("base_url"):
    model_cfg["base_url"] = PROVIDER_BASE[chosen]
if not model_cfg.get("default") and chosen in PROVIDER_DEFAULT_MODEL:
    model_cfg["default"] = PROVIDER_DEFAULT_MODEL[chosen]
cfg["model"] = model_cfg
yaml.safe_dump(cfg, cfg_p.open("w"), sort_keys=False)
print(f"[entrypoint] wrote config.yaml: model.provider={chosen}, default={model_cfg.get('default')!r}")
PY
fi

# Self-heal: backfill stale `model: <other-provider>/<name>` and missing
# model_provider on existing webui sessions, so users don't hit "No LLM
# provider configured" when reopening sessions created before they configured
# the active provider.
SESSIONS_DIR="${HERMES_HOME}/.hermes/webui/sessions"
if [ -d "${SESSIONS_DIR}" ] && [ -f "${HERMES_HOME}/config.yaml" ]; then
  python3 - <<PY || true
import json, glob, os, pathlib
import yaml
cfg = yaml.safe_load(pathlib.Path("${HERMES_HOME}/config.yaml").read_text()) or {}
model_cfg = cfg.get("model") or {}
default_model = (model_cfg.get("default") or "").strip()
default_provider = (model_cfg.get("provider") or "").strip()
if not (default_model and default_provider):
    raise SystemExit(0)

patched = 0
for p in glob.glob("${SESSIONS_DIR}/*.json"):
    try:
        s = json.loads(open(p).read())
    except Exception:
        continue
    if not isinstance(s, dict):
        continue
    m = s.get("model") or ""
    needs_fix = (
        not s.get("model_provider")
        or ("/" in m and not m.startswith(default_provider + "/"))
    )
    if needs_fix:
        s["model"] = default_model
        s["model_provider"] = default_provider
        json.dump(s, open(p, "w"))
        patched += 1

idx_p = pathlib.Path("${SESSIONS_DIR}/_index.json")
if idx_p.exists():
    try:
        idx = json.loads(idx_p.read_text())
        idx_patched = 0
        for e in idx:
            if not isinstance(e, dict):
                continue
            m = e.get("model") or ""
            if not e.get("model_provider") or ("/" in m and not m.startswith(default_provider + "/")):
                e["model"] = default_model
                e["model_provider"] = default_provider
                idx_patched += 1
        idx_p.write_text(json.dumps(idx))
        print(f"[entrypoint] backfilled {patched} sessions, {idx_patched} index entries")
    except Exception:
        pass
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
WEBUI_PID_FILE="${HERMES_HOME}/.hermes/webui/server.pid"
mkdir -p "$(dirname "${WEBUI_PID_FILE}")"

# Respawn watchdog: keeps hermes-webui alive. Lets /auth-cli kill it after a
# successful auth/model command so the user gets a fresh process (with
# refreshed catalog/session cache) without needing a full container restart.
# Don't use setsid — it detaches the child so `wait $!` can't track it,
# turning the loop into a tight respawn that breaks within a few iterations.
start_webui_watchdog() {
  local AS_USER=$1
  set +e
  while true; do
    printf '\n--- Starting Hermes WebUI on 127.0.0.1:9119 %s ---\n' "$(date)" >> "${WEBUI_LOG}"
    if [ -n "${AS_USER}" ]; then
      gosu "${AS_USER}" python3 /opt/hermes-webui/server.py >> "${WEBUI_LOG}" 2>&1 < /dev/null &
    else
      python3 /opt/hermes-webui/server.py >> "${WEBUI_LOG}" 2>&1 < /dev/null &
    fi
    local PID=$!
    echo "${PID}" > "${WEBUI_PID_FILE}"
    echo "Hermes WebUI PID: ${PID}  (logs at ${WEBUI_LOG})"
    wait "${PID}"
    local RC=$?
    echo "[entrypoint] Hermes WebUI (pid ${PID}) exited with code ${RC}; respawning in 2s" >> "${WEBUI_LOG}"
    rm -f "${WEBUI_PID_FILE}"
    # Wipe the catalog cache before respawning so the new process picks up
    # any config.yaml / auth.json changes that triggered the restart.
    rm -f "${HERMES_HOME}/.hermes/webui/models_cache.json" 2>/dev/null || true
    sleep 2
  done
}

if [ "$(id -u)" = "0" ]; then
  chown -R hermes:hermes "${HERMES_HOME}" /opt/hermes-railway 2>/dev/null || true
  start_webui_watchdog hermes &
  exec gosu hermes python3 -m uvicorn admin.app:app --host 0.0.0.0 --port "${PORT}"
fi

start_webui_watchdog "" &
exec python3 -m uvicorn admin.app:app --host 0.0.0.0 --port "${PORT}"
