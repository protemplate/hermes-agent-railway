FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie

ARG HERMES_REF=main

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright \
    HERMES_HOME=/data \
    HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist \
    PATH="/opt/hermes/.venv/bin:/data/.local/bin:${PATH}" \
    PYTHONPATH="/opt/hermes-railway:/opt/hermes"

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

RUN useradd --system --uid 10000 --create-home --home-dir /home/hermes --shell /bin/bash hermes

WORKDIR /opt/hermes

RUN git init . && \
    git remote add origin https://github.com/NousResearch/hermes-agent.git && \
    (git fetch --depth 1 origin "${HERMES_REF}" || git fetch --depth 1 origin "refs/tags/${HERMES_REF}:refs/tags/${HERMES_REF}") && \
    git checkout --detach FETCH_HEAD

ENV npm_config_install_links=false

RUN npm install --prefer-offline --no-audit && \
    npx playwright install --with-deps chromium --only-shell && \
    (cd web && npm install --prefer-offline --no-audit) && \
    (cd ui-tui && npm install --prefer-offline --no-audit) && \
    npm cache clean --force

RUN cd web && npm run build && \
    cd ../ui-tui && npm run build

RUN uv venv && \
    uv pip install --no-cache-dir -e ".[all]" && \
    uv pip install --no-cache-dir ptyprocess httpx websockets

RUN chmod -R a+rX /opt/hermes

WORKDIR /opt/hermes-railway

COPY admin ./admin
COPY skills ./skills
COPY entrypoint.sh ./entrypoint.sh

RUN chmod +x /opt/hermes-railway/entrypoint.sh && \
    mkdir -p /data && \
    chown -R hermes:hermes /data /opt/hermes-railway

EXPOSE 8080
# Internal port 9119 is used by Hermes's native web dashboard (not exposed by EXPOSE).

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -f "http://localhost:${PORT:-8080}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/hermes-railway/entrypoint.sh"]
