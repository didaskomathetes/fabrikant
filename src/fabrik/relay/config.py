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
            errs.append(f"relay.{where}: Mapping mit backend/model erwartet")
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
            errs.append("relay.trail_archive: Mapping mit `type` erwartet")
            return None
        kind = raw.get("type")
        if kind in TRAIL_ARCHIVE_PLANNED:
            errs.append(f"relay.trail_archive.type: {kind!r} ist vorgesehen, aber noch nicht umgesetzt")
            return None
        if kind not in TRAIL_ARCHIVE_TYPES:
            errs.append(f"relay.trail_archive.type: eins von {', '.join(TRAIL_ARCHIVE_TYPES)} (ist {kind!r})")
            return None
        return cls(type=str(kind))


def _str_list(raw: Any, where: str, errs: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        errs.append(f"relay.{where}: Liste nicht-leerer Strings erwartet")
        return ()
    return tuple(x.strip() for x in raw)


def _positive(raw: Any, default: int, where: str, errs: list[str]) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        errs.append(f"relay.{where}: positive Zahl erwartet (ist {raw!r})")
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
            raise ValueError("relay: Mapping erwartet")
        known = {f for f in cls.__dataclass_fields__ if f not in ("project", "dispatch_cron")} | {"dispatch"}
        errs += [f"relay.{k}: unbekannter Key" for k in raw if k not in known]
        repo = raw.get("repo") or spec.get("github")
        if not isinstance(repo, str) or "/" not in repo:
            errs.append("relay.repo (oder github: auf Spec-Ebene) als owner/name nötig")
        allowed = _str_list(raw.get("allowed"), "allowed", errs)
        if not allowed:
            errs.append("relay.allowed: mindestens ein GitHub-Login (Briefs sind Agent-Anweisungen)")
        bot_login = raw.get("bot_login")
        if not isinstance(bot_login, str) or not bot_login.strip():
            errs.append("relay.bot_login: Pflicht (Status-Kommentare nur von der gepinnten Identität)")
        audit = raw.get("audit", True)
        if not isinstance(audit, bool):
            errs.append("relay.audit muss true/false sein")
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
            "description": f"Relay-Dispatcher: startet die nächste fällige Session/das Audit für `{cfg.label}`-Issues "
            "(kein Agent, keine Kosten ohne fällige Tasks)",
            "tier": 1,
            "worktree": False,
            "triggers": [{"type": "cron", "cron": cfg.dispatch_cron}],
            "steps": [_shell("dispatch", "dispatch", 300, issue=False)],
        },
        LOOP_SESSION: {
            "description": "Relay-Session: Brief + Handoff → Agent arbeitet bis zur Deadline → Check-in "
            "(Verify, Scope, Stall) → Commit + Push auf fabrik/relay/<N> → Status-Kommentar",
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
            "description": "Relay-Audit (Reviewer, read-only): Phase 1 blind eigene Reconciliation, "
            "Phase 2 Prüfung der Session-Skripte/-Logs → AUDIT.md + Verdikt",
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
        raise ValueError(f"relay: Loop-Namen {clash} sind für Relay reserviert")
    loops.update(build_loops(cfg))


def load(project: str) -> tuple[dict, RelayConfig]:
    """Spec + RelayConfig eines Projekts (für die Subcommands)."""
    from .. import specs  # spät: specs importiert dieses Modul

    spec = specs.load_spec(specs.spec_path(project))
    if spec.get("relay") is None:
        raise ValueError(f"Projekt {project!r} hat keinen relay:-Block")
    return spec, RelayConfig.from_spec(spec)
