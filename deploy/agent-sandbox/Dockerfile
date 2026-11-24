# Image for the per-run agent sandbox (ADR 0054): one-shot CLI agents (claude-code, codex,
# copilot, opencode) run inside it with `FABRIKANT_SANDBOX=1`.
#
#   docker build -t fabrikant/agent-sandbox:latest deploy/agent-sandbox
#
# What fabrikant requires (sandbox.render_docker_cmd / entrypoint_script):
#   - POSIX `sh` and `git` (exchange via git bundle), the agent CLIs on PATH;
#   - read-only root FS: everything writable comes as tmpfs (`/tmp`, HOME `/home/agent`, `/work`)
#     or mount (`/in` ro, `/out`) — the image only creates the mount points;
#   - arbitrary uid (`-u` = host uid or 10001): nothing in the image may assume a particular user;
#     fabrikant sets HOME via `-e HOME=/home/agent`;
#   - no network except the egress proxy (ADR 0051): nothing is installed at runtime.
#
# Updates: bump the versions below deliberately (`--build-arg` to try them out) and rebuild
# the image; fabrikant starts it with `--pull never`.

# opencode 2.x is not on npm (only 1.x there): release binary from GitHub, verified by SHA-256.
# Separate stage so curl does not end up in the agent image. Base pinned by digest.
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS opencode
ARG OPENCODE_VERSION=2.2.5
ARG OPENCODE_SHA256=09f44302c10fd036912f59c81fd1aef5df45dd06f649197f9dfc9bbe2a79614e
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL -o /tmp/opencode.tar.gz \
        "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" \
    && echo "${OPENCODE_SHA256}  /tmp/opencode.tar.gz" | sha256sum -c - \
    && mkdir /opt/opencode && tar -xzf /tmp/opencode.tar.gz -C /opt/opencode \
    && find /opt/opencode -maxdepth 2 -type f -name opencode -exec install -m 0755 {} /usr/local/bin/opencode \;

# Base: Node (agent CLIs are npm packages), pinned by digest.
FROM node:24-bookworm-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9

ARG CLAUDE_CODE_VERSION=2.1.322
ARG CODEX_VERSION=0.167.0
ARG COPILOT_VERSION=1.0.108
# Python for target repos with `requires-python` beyond Debian's python3 (e.g. 3.13).
ARG PYTHON_VERSION=3.13.13

# Tools agents typically need in target repos: git (mandatory), gh, ripgrep,
# Python 3 for tests/scripts. `ca-certificates` for TLS through the proxy (CONNECT, no MITM).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git gh ripgrep python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# uv (Python projects), pinned by digest.
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /uvx /usr/local/bin/

# Bake the interpreter into the image instead of downloading it at runtime (the downloads are not on the
# egress allowlist). Location outside HOME (tmpfs) and readable by every uid.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_DOWNLOADS=never
RUN UV_PYTHON_DOWNLOADS=automatic uv python install "${PYTHON_VERSION}" \
    && chmod -R a+rX /opt/uv

RUN npm install -g --omit=dev --no-fund --no-audit \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "@github/copilot@${COPILOT_VERSION}" \
    && npm cache clean --force

COPY --from=opencode /usr/local/bin/opencode /usr/local/bin/opencode

# Mount points (read-only root FS: docker would otherwise create them at runtime; explicit here).
# /home/agent and /work are tmpfs with mode=1777 (render_docker_cmd), /out is 0777 on the host.
RUN mkdir -p /in /out /work /home/agent \
    && chmod 1777 /work /home/agent

# No auto-update/telemetry from the sandbox (no network for it, read-only FS).
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    COPILOT_AUTO_UPDATE=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_MODELS_FETCH=1

# Default user; fabrikant overrides it with `-u` (host uid or 10001).
USER 10001:10001
WORKDIR /work

# fabrikant sets `--entrypoint sh` and the exchange entrypoint itself; without fabrikant: show versions.
CMD ["sh", "-c", "git --version && claude --version && codex --version && copilot --version && opencode --version"]
