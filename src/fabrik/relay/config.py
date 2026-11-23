"""`relay:`-Block einer Projekt-Spec → `RelayConfig` + generierte Loops (ADR 0037).

Ein Projekt schaltet Relay-Tasks mit einem `relay:`-Block frei; `expand_loops` erzeugt daraus
beim Spec-Laden die Loops `relay-dispatch`, `relay-session` und `relay-audit`. Die Prompts
(`prompts/relay/`) sind projektneutral — Projektwissen kommt aus Brief und Repo.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from typing import Any

from .. import config
from ..i18n import t
from ..retry import RetryPolicy
from .model import Labels

LOOP_DISPATCH = "relay-dispatch"
LOOP_SESSION = "relay-session"
LOOP_AUDIT = "relay-audit"
RUN_LOOPS = (LOOP_SESSION, LOOP_AUDIT)

PROMPT_SESSION = "prompts/relay/session.prompt.md"
PROMPT_AUDIT_BLIND = "prompts/relay/audit-blind.prompt.md"
PROMPT_AUDIT_REVIEW = "prompts/relay/audit-review.prompt.md"

DEFAULT_DISPATCH_CRON = "*/15 * * * *"
# Obergrenze für `session_minutes` aus dem Brief; Session-Run muss unter FABRIK_STALE_HOURS bleiben.
DEFAULT_MAX_SESSION_MINUTES = 35
DEFAULT_VERIFY_TIMEOUT = 300
# Zwei Audit-Phasen + Steps müssen unter FABRIK_STALE_HOURS (lokal 1 h) bleiben.
DEFAULT_AUDIT_TIMEOUT = 1200
# Puffer zwischen hartem Session-Ende (Prompt) und Agent-Timeout (Kill).
AGENT_TIMEOUT_GRACE_S = 300
FABRIK_URL_ENV = "FABRIK_URL"
# Archiv-Methoden für den Trail (ADR 0037, Nachtrag 2026-11-03); s3 ist vorgesehen, nicht umgesetzt.
TRAIL_ARCHIVE_TYPES = ("git-ref", "local")
TRAIL_ARCHIVE_PLANNED = ("s3",)


def fabrik_url() -> str:
    """Basis-URL der laufenden fabrik-API (der Dispatcher startet Runs darüber)."""
    return os.environ.get(FABRIK_URL_ENV, f"http://127.0.0.1:{config.DEFAULT_PORT}").rstrip("/")


@dataclass(frozen=True)
class AgentRole:
    """Backend/Modell einer Relay-Rolle (Builder = Sessions, Reviewer = Audit)."""

    backend: str | None = None
    model: str | None = None

    @classmethod
    def parse(cls, raw: Any, where: str, errs: list[str]) -> AgentRole:
        if raw is None:
            return cls()
        if not isinstance(raw, dict) or set(raw) - {"backend", "model"}:
            errs.append(t("relay.config.relay_mapping_with_backend_model", where=where))
            return cls()
        return cls(backend=raw.get("backend"), model=raw.get("model"))

    def step_fields(self) -> dict[str, str]:
        return {k: v for k, v in (("backend", self.backend), ("model", self.model)) if v}


@dataclass(frozen=True)
class TrailArchiveConfig:
    """`relay.trail_archive`: wohin der Trail nach `pass` archiviert wird, bevor er vom Branch geht."""

    type: str

    @classmethod
    def parse(cls, raw: Any, errs: list[str]) -> TrailArchiveConfig | None:
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) - {"type"}:
            errs.append(t("relay.config.relay_trail_archive_mapping_with"))
            return None
        kind = raw.get("type")
        if kind in TRAIL_ARCHIVE_PLANNED:
            errs.append(t("relay.config.relay_trail_archive_type_is", kind=kind))
            return None
        if kind not in TRAIL_ARCHIVE_TYPES:
            errs.append(t("relay.config.relay_trail_archive_type_one", v1=", ".join(TRAIL_ARCHIVE_TYPES), kind=kind))
            return None
        return cls(type=str(kind))


def _str_list(raw: Any, where: str, errs: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        errs.append(t("relay.config.relay_list_of_non_empty", where=where))
        return ()
    return tuple(x.strip() for x in raw)


def _positive(raw: Any, default: int, where: str, errs: list[str]) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        errs.append(t("relay.config.relay_positive_number_expected_is", where=where, raw=raw))
        return default
    return raw


@dataclass(frozen=True)
class RelayConfig:
    """Typisierter `relay:`-Block (Defaults projektneutral, `allowed` ist Pflicht)."""

    project: str
    repo: str
    allowed: frozenset[str]
    base: str = "main"
    label: str = Labels.task
    forbid: tuple[str, ...] = ()
    dispatch_cron: str = DEFAULT_DISPATCH_CRON
    builder: AgentRole = AgentRole()
    reviewer: AgentRole = AgentRole()
    max_session_minutes: int = DEFAULT_MAX_SESSION_MINUTES
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT
    audit_timeout: int = DEFAULT_AUDIT_TIMEOUT
    # Pflicht (from_spec validiert); `""` nur als Dataclass-Default für Teil-Konstruktion in Tests.
    bot_login: str = ""
    audit: bool = True
    # Wiederholung gescheiterter Relay-Runs (Semantik wie Run-Retry, ADR 0036); None = jeder Fehlschlag blockiert.
    retry: RetryPolicy | None = None
    # None = Trail bleibt auf dem Branch (Status-Kommentar warnt, wenn ein PR existiert).
    trail_archive: TrailArchiveConfig | None = None

    @property
    def labels(self) -> Labels:
        return Labels(task=self.label)

    @property
    def session_timeout(self) -> int:
        return self.max_session_minutes * 60 + AGENT_TIMEOUT_GRACE_S

    @classmethod
    def from_spec(cls, spec: dict) -> RelayConfig:
        """ValueError mit allen Fehlern, wenn der Block ungültig ist."""
        raw = spec.get("relay")
        errs: list[str] = []
        if not isinstance(raw, dict):
            raise ValueError(t("relay.config.relay_mapping_expected"))
        known = {f for f in cls.__dataclass_fields__ if f not in ("project", "dispatch_cron")} | {"dispatch"}
        errs += [f"relay.{k}: unbekannter Key" for k in raw if k not in known]
        repo = raw.get("repo") or spec.get("github")
        if not isinstance(repo, str) or "/" not in repo:
            errs.append(t("relay.config.relay_repo_or_github_at"))
        allowed = _str_list(raw.get("allowed"), "allowed", errs)
        if not allowed:
            errs.append(t("relay.config.relay_allowed_at_least_one"))
        bot_login = raw.get("bot_login")
        if not isinstance(bot_login, str) or not bot_login.strip():
            errs.append(t("relay.config.relay_bot_login_required_status"))
        audit = raw.get("audit", True)
        if not isinstance(audit, bool):
            errs.append(t("relay.config.relay_audit_must_be_true"))
        cfg = cls(
            project=str(spec.get("name", "")),
            repo=str(repo),
            allowed=frozenset(allowed),
            base=str(raw.get("base") or (spec.get("repo") or {}).get("branch") or "main"),
            label=str(raw.get("label") or Labels.task),
            forbid=_str_list(raw.get("forbid"), "forbid", errs),
            dispatch_cron=str(raw.get("dispatch") or DEFAULT_DISPATCH_CRON),
            builder=AgentRole.parse(raw.get("builder"), "builder", errs),
            reviewer=AgentRole.parse(raw.get("reviewer"), "reviewer", errs),
            max_session_minutes=_positive(
                raw.get("max_session_minutes"), DEFAULT_MAX_SESSION_MINUTES, "max_session_minutes", errs
            ),
            verify_timeout=_positive(raw.get("verify_timeout"), DEFAULT_VERIFY_TIMEOUT, "verify_timeout", errs),
            audit_timeout=_positive(raw.get("audit_timeout"), DEFAULT_AUDIT_TIMEOUT, "audit_timeout", errs),
            bot_login=str(bot_login).strip() if isinstance(bot_login, str) else "",
            audit=bool(audit),
            retry=RetryPolicy.parse(raw.get("retry"), "relay.retry", errs),
            trail_archive=TrailArchiveConfig.parse(raw.get("trail_archive"), errs),
        )
        if errs:
            raise ValueError("; ".join(errs))
        return cfg


def _cmd(command: str, *args: str) -> str:
    """Shell-Kommando für einen Relay-Subcommand (gleicher Interpreter wie der Server).

    Die Issue-Nummer kommt über `env:` ($RELAY_ISSUE), nie inline (ADR 0008)."""
    return " ".join([shlex.quote(sys.executable), "-m", "fabrik.relay", command, "--project", "{project}", *args])


_ISSUE_ARG = '--issue "$RELAY_ISSUE"'
_ISSUE_ENV = {"RELAY_ISSUE": "{issue_number}"}
_BRANCH = "fabrik/relay/{issue_number}"


def _shell(name: str, command: str, timeout: int, *, issue: bool = True) -> dict:
    step: dict[str, Any] = {"name": name, "type": "shell", "timeout": timeout}
    if issue:
        step |= {"cmd": _cmd(command, _ISSUE_ARG), "env": dict(_ISSUE_ENV)}
    else:
        step["cmd"] = _cmd(command)
    return step


def _agent(name: str, role: AgentRole, identity: str, prompt: str, timeout: int) -> dict:
    # gate: false — Check-in/Publish laufen auch nach Timeout/Fehler des Agents (Handoff sichern).
    return {
        "name": name,
        "type": "agent",
        **role.step_fields(),
        "github_identity": identity,
        "prompt_file": prompt,
        "timeout": timeout,
        "gate": False,
    }


def build_loops(cfg: RelayConfig) -> dict[str, dict]:
    """Die Relay-Loops eines Projekts (Specs-Dicts im normalen Loop-Format)."""
    loops = {
        LOOP_DISPATCH: {
            "description": t("relay.config.relay_dispatcher_starts_the_next", v1=cfg.label),
            "tier": 1,
            "worktree": False,
            "triggers": [{"type": "cron", "cron": cfg.dispatch_cron}],
            "steps": [_shell("dispatch", "dispatch", 300, issue=False)],
        },
        LOOP_SESSION: {
            "description": t("relay.config.relay_session_brief_handoff_agent"),
            "tier": 1,
            "triggers": [{"type": "manual"}],
            "steps": [
                {"name": "branch", "type": "git", "fetch": True, "branch": _BRANCH},
                _shell("checkout", "checkout", 120),
                _agent("session", cfg.builder, "dev", PROMPT_SESSION, cfg.session_timeout),
                _shell("checkin", "checkin", cfg.verify_timeout + 120),
                _shell("publish", "publish", 300),
            ],
        },
    }
    if cfg.audit:
        loops[LOOP_AUDIT] = {
            "description": t("relay.config.relay_audit_reviewer_read_only"),
            "tier": 1,
            "triggers": [{"type": "manual"}],
            "steps": [
                {"name": "branch", "type": "git", "fetch": True, "branch": _BRANCH},
                _shell("audit-prepare", "audit-prepare", 120),
                _agent("audit-blind", cfg.reviewer, "reviewer", PROMPT_AUDIT_BLIND, cfg.audit_timeout),
                _shell("audit-reveal", "audit-reveal", 120),
                _agent("audit-review", cfg.reviewer, "reviewer", PROMPT_AUDIT_REVIEW, cfg.audit_timeout),
                _shell("audit-publish", "audit-publish", 300),
            ],
        }
    return loops


def expand_loops(spec: dict) -> None:
    """`relay:` → Relay-Loops in `spec["loops"]` einfügen (vor der Spec-Validierung)."""
    if spec.get("relay") is None:
        return
    cfg = RelayConfig.from_spec(spec)
    loops = spec.get("loops")
    if loops is None:
        loops = spec["loops"] = {}
    if not isinstance(loops, dict):
        return  # Spec-Validierung meldet den Fehler
    clash = sorted(set(loops) & {LOOP_DISPATCH, *RUN_LOOPS})
    if clash:
        raise ValueError(t("relay.config.relay_loop_names_are_reserved", clash=clash))
    loops.update(build_loops(cfg))


def load(project: str) -> tuple[dict, RelayConfig]:
    """Spec + RelayConfig eines Projekts (für die Subcommands)."""
    from .. import specs  # spät: specs importiert dieses Modul

    spec = specs.load_spec(specs.spec_path(project))
    if spec.get("relay") is None:
        raise ValueError(t("relay.config.project_has_no_relay_block", project=project))
    return spec, RelayConfig.from_spec(spec)
