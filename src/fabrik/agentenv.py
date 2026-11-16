"""Agent-Env-Allowlist — Agent-Prozesse bekommen nur Variablen, die sie brauchen (ADR 0050).

Begründung: Eine Prompt-Injection kann den Agent-Prozessen eigene Umgebungsvariablen
auslesen und exfiltrieren. Vor ADR 0050 erbt jeder CLI-Agent-Prozess das komplette
`os.environ` — darunter `GITHUB_TOKEN` und Provider-API-Keys. Jetzt bekommen Agent-Prozesse
nur die Allowlist plus die Variablen, die pro Step explizit übergeben werden
(`extra_env`, z. B. die `gh`-Identität eines `github_identity:`-Steps).

Der geteilte `opencode serve`-Prozess wird nicht von fabrik gestartet — für ihn gibt
`fabrik agent-env` die gescrubte Env als `KEY=VALUE`-Zeilen aus (nutzbar als
systemd-`EnvironmentFile` oder Docker `--env-file`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# Namen, die Agent-Prozesse immer bekommen (bewusst ohne Secrets/Provider-Keys).
AGENT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "HOSTNAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
)

# Eigene Auth-Variablen der Agent-CLIs (ihre Arbeits-Identität, keine geerbten fabrik-Secrets).
# Backends reichen sie über `_env()` explizit durch (`passthrough`) — nie `GITHUB_TOKEN`
# (das ist fabriks API-Token).
CLAUDE_AUTH_VARS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
CODEX_AUTH_VARS: tuple[str, ...] = ("OPENAI_API_KEY",)
COPILOT_AUTH_VARS: tuple[str, ...] = ("COPILOT_GITHUB_TOKEN",)

# Env-Name für die Operator-Ergänzung (kommagetrennte Namen).
EXTRA_ALLOWLIST_VAR = "FABRIK_AGENT_ENV_EXTRA"


def extra_allowlist_names() -> frozenset[str]:
    """Zusätzliche Allowlist-Namen aus `FABRIK_AGENT_ENV_EXTRA` (kommagetrennt, exakt)."""
    raw = os.environ.get(EXTRA_ALLOWLIST_VAR, "")
    return frozenset(n.strip() for n in raw.split(",") if n.strip())


def passthrough(names: tuple[str, ...]) -> dict[str, str]:
    """Die gesetzten Variablen aus `names` (aus `os.environ`) — bewusste Übergabe der
    CLI-eigenen Auth, nicht Erbschaft (ADR 0050)."""
    return {n: os.environ[n] for n in names if os.environ.get(n)}


def build_agent_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Vollständige Env für einen Agent-Prozess: Allowlist (aus `os.environ`) + `extra`.

    `extra` (Adapter-Zusätze wie `COPILOT_AUTO_UPDATE` oder pro-Step-`extra_env`, z. B. die
    `gh`-Identität) wird über die Allowlist gelegt und gewinnt dabei immer. Nur Namen aus
    der Allowlist werden aus `os.environ` übernommen — alles andere (Secrets,
    Provider-Keys, `FABRIK_*`-Konfiguration) bleibt draußen.
    """
    allowed = set(AGENT_ENV_ALLOWLIST) | extra_allowlist_names()
    env = {k: v for k, v in os.environ.items() if k in allowed}
    if extra:
        env.update(extra)
    return env
