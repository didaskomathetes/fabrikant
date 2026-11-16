# Image für die Per-Run-Agent-Sandbox (ADR 0054): One-Shot-CLI-Agenten (claude-code, codex,
# copilot) laufen darin mit `FABRIK_SANDBOX=1`.
#
#   docker build -t fabrik/agent-sandbox:latest deploy/agent-sandbox
#
# Was fabrik voraussetzt (sandbox.render_docker_cmd / entrypoint_script):
#   - POSIX `sh` und `git` (Austausch per git-Bundle), die Agent-CLIs im PATH;
#   - read-only Root-FS: alles Schreibbare kommt als tmpfs (`/tmp`, HOME `/home/agent`, `/work`)
#     bzw. Mount (`/in` ro, `/out`) — das Image legt nur die Mountpunkte an;
#   - beliebige uid (`-u` = Host-uid bzw. 10001): nichts im Image darf einen bestimmten User
#     voraussetzen; HOME setzt fabrik per `-e HOME=/home/agent`;
#   - kein Netz außer dem Egress-Proxy (ADR 0051): zur Laufzeit wird nichts nachinstalliert.
#
# Updates: Versionen unten bewusst anheben (`--build-arg` zum Ausprobieren) und das Image neu
# bauen; fabrik startet es mit `--pull never`.

# Basis: Node (Agent-CLIs sind npm-Pakete), per Digest gepinnt.
FROM node:24-bookworm-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9

ARG CLAUDE_CODE_VERSION=2.1.322
ARG CODEX_VERSION=0.167.0
ARG COPILOT_VERSION=1.0.108
# Python für Ziel-Repos mit `requires-python` jenseits von Debians python3 (ai-lab: 3.13).
ARG PYTHON_VERSION=3.13.13

# Werkzeuge, die Agenten in Ziel-Repos typischerweise brauchen: git (Pflicht), gh, ripgrep,
# Python 3 für Tests/Skripte. `ca-certificates` für TLS durch den Proxy (CONNECT, kein MITM).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git gh ripgrep python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# uv (Python-Projekte wie ai-lab-merchant-assistant), per Digest gepinnt.
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /uvx /usr/local/bin/

# Interpreter ins Image statt zur Laufzeit laden (die Downloads liegen nicht auf der
# Egress-Allowlist). Ort außerhalb von HOME (tmpfs) und lesbar für jede uid.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_DOWNLOADS=never
RUN UV_PYTHON_DOWNLOADS=automatic uv python install "${PYTHON_VERSION}" \
    && chmod -R a+rX /opt/uv

RUN npm install -g --omit=dev --no-fund --no-audit \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "@github/copilot@${COPILOT_VERSION}" \
    && npm cache clean --force

# Mountpunkte (read-only Root-FS: docker legt sie sonst zur Laufzeit an, hier explizit).
# /home/agent und /work sind tmpfs mit mode=1777 (render_docker_cmd), /out ist 0777 auf dem Host.
RUN mkdir -p /in /out /work /home/agent \
    && chmod 1777 /work /home/agent

# Kein Auto-Update/keine Telemetrie aus der Sandbox (kein Netz dafür, read-only FS).
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    COPILOT_AUTO_UPDATE=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false

# Default-User; fabrik überschreibt ihn mit `-u` (Host-uid bzw. 10001).
USER 10001:10001
WORKDIR /work

# fabrik setzt `--entrypoint sh` und den Austausch-Entrypoint selbst; ohne fabrik: Versionen zeigen.
CMD ["sh", "-c", "git --version && claude --version && codex --version && copilot --version"]
