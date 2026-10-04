FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie-slim

ARG HERMES_REF=v2026.6.5
ARG HERMES_WEBUI_REF=v0.51.310

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright \
    HERMES_HOME=/data \
    PATH="/opt/hermes/.venv/bin:/data/.local/bin:${PATH}" \
    PYTHONPATH="/opt/hermes-railway:/opt/hermes:/opt/hermes-webui"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      curl \
      docker-cli \
      ffmpeg \
      gcc \
      git \
      gosu \
      libffi-dev \
      nodejs \
      npm \
      openssh-client \
      procps \
      python3 \
      python3-dev \
      ripgrep \
      tini && \
    rm -rf /var/lib/apt/lists/*

# Railway's Trial and Free plans reject images over 4 GB. Keep this image well under it:
# the app directories are owned by `hermes` before anything is written into them, and
# everything below is installed as that user. A `chown -R` afterwards would copy every
# file into a new layer (that alone took the image from 4.5 GB to 6.8 GB).
# Owned by `hermes` so `git fetch`/`hermes update` from the Web TUI do not trip
# "detected dubious ownership", and so Hermes can build its web UI and TUI on first use.
RUN useradd --system --uid 10000 --create-home --home-dir /home/hermes --shell /bin/bash hermes && \
    mkdir -p /opt/hermes /opt/hermes-webui /opt/hermes-railway /data && \
    chown hermes:hermes /opt/hermes /opt/hermes-webui /opt/hermes-railway /data

USER hermes
WORKDIR /opt/hermes

RUN git init . && \
    git remote add origin https://github.com/NousResearch/hermes-agent.git && \
    (git fetch --depth 1 origin "${HERMES_REF}" || git fetch --depth 1 origin "refs/tags/${HERMES_REF}:refs/tags/${HERMES_REF}") && \
    git checkout --detach FETCH_HEAD

ENV npm_config_install_links=false

# Root dependencies (agent-browser) plus the web dashboard and TUI workspaces, which
# Hermes builds with npm on first use. The apps/* workspaces are skipped: they are the
# Electron desktop app and its installers (about 600 MB), used only by `hermes desktop`.
RUN npm install --prefer-offline --no-audit --include-workspace-root \
      --workspace web \
      --workspace ui-tui \
      --workspace ui-tui/packages/hermes-ink && \
    npm cache clean --force && \
    rm -rf /home/hermes/.cache

# Chromium's system libraries need root; the browser itself goes to
# PLAYWRIGHT_BROWSERS_PATH and is handed to `hermes` in the same layer.
USER root
RUN npx playwright install --with-deps chromium --only-shell && \
    chown -R hermes:hermes /opt/hermes/.playwright && \
    rm -rf /var/lib/apt/lists/* /root/.npm /root/.cache
USER hermes

RUN uv venv && \
    uv pip install --no-cache-dir -e ".[all,messaging]"

# Hermes WebUI: pure Python (stdlib + pyyaml) + vanilla JS, served from /opt/hermes-webui
WORKDIR /opt/hermes-webui

RUN git init . && \
    git remote add origin https://github.com/nesquena/hermes-webui.git && \
    (git fetch --depth 1 origin "refs/tags/${HERMES_WEBUI_REF}:refs/tags/${HERMES_WEBUI_REF}" || git fetch --depth 1 origin "${HERMES_WEBUI_REF}") && \
    git checkout --detach FETCH_HEAD && \
    uv pip install --python /opt/hermes/.venv/bin/python --no-cache-dir -r requirements.txt

# Wrapper extras: small Starlette app that exposes /tui (in-browser xterm with
# OAuth shortcut buttons for `hermes auth add` device-code flows plus a free-
# form `/bin/bash` pane) and reverse-proxies everything else to hermes-webui on
# loopback (HERMES_WEBUI_HOST / HERMES_WEBUI_PORT; default 127.0.0.1:9120).
# starlette/uvicorn/httpx may already be transitive Hermes
# deps, but install explicitly to pin.
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache-dir \
    ptyprocess httpx websockets starlette uvicorn

# The entrypoint starts as root (it fixes volume ownership, then drops to `hermes`).
USER root
WORKDIR /opt/hermes-railway

COPY --chown=hermes:hermes admin ./admin
COPY --chown=hermes:hermes skills ./skills
COPY --chown=hermes:hermes entrypoint.sh ./entrypoint.sh

RUN chmod +x /opt/hermes-railway/entrypoint.sh && \
    git config --system --add safe.directory /opt/hermes && \
    git config --system --add safe.directory /opt/hermes-webui

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -f "http://localhost:${PORT:-8080}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/hermes-railway/entrypoint.sh"]
