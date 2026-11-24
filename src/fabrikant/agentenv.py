"""Agent env allowlist — agent processes only get the variables they need (ADR 0050).

Rationale: a prompt injection can read and exfiltrate the agent processes' own environment
variables. Before ADR 0050 every CLI agent process inherited the complete
`os.environ` — including `GITHUB_TOKEN` and provider API keys. Now agent processes get
only the allowlist plus the variables passed explicitly per step
(`extra_env`, e.g. the `gh` identity of a `github_identity:` step).

The shared `opencode serve` process is not started by fabrikant — for it,
`fabrikant agent-env` prints the scrubbed env as `KEY=VALUE` lines (usable as a
systemd `EnvironmentFile` or Docker `--env-file`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# Names agent processes always get (deliberately without secrets/provider keys).
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

# The agent CLIs' own auth variables (their working identity, no inherited fabrikant secrets).
# Backends pass them through explicitly via `_env()` (`passthrough`) — never `GITHUB_TOKEN`
# (that is fabrikant's API token).
CLAUDE_AUTH_VARS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
CODEX_AUTH_VARS: tuple[str, ...] = ("OPENAI_API_KEY",)
COPILOT_AUTH_VARS: tuple[str, ...] = ("COPILOT_GITHUB_TOKEN",)

# Env name for the operator addition (comma-separated names).
EXTRA_ALLOWLIST_VAR = "FABRIKANT_AGENT_ENV_EXTRA"


def extra_allowlist_names() -> frozenset[str]:
    """Additional allowlist names from `FABRIKANT_AGENT_ENV_EXTRA` (comma-separated, exact)."""
    raw = os.environ.get(EXTRA_ALLOWLIST_VAR, "")
    return frozenset(n.strip() for n in raw.split(",") if n.strip())


def passthrough(names: tuple[str, ...]) -> dict[str, str]:
    """The set variables from `names` (from `os.environ`) — deliberate hand-over of the
    CLI's own auth, not inheritance (ADR 0050)."""
    return {n: os.environ[n] for n in names if os.environ.get(n)}


def build_agent_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Complete env for an agent process: allowlist (from `os.environ`) + `extra`.

    `extra` (adapter additions like `COPILOT_AUTO_UPDATE` or per-step `extra_env`, e.g. the
    `gh` identity) is laid over the allowlist and always wins. Only names from the
    allowlist are taken from `os.environ` — everything else (secrets,
    provider keys, `FABRIKANT_*` configuration) stays out.
    """
    allowed = set(AGENT_ENV_ALLOWLIST) | extra_allowlist_names()
    env = {k: v for k, v in os.environ.items() if k in allowed}
    if extra:
        env.update(extra)
    return env
