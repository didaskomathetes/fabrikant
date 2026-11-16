"""Zusätzlicher Prompt-Kontext für LLM-Knoten (`extra_context`).

Gilt für alle Step-Typen in `LLM_STEP_TYPES`, auch als Check in einem `verify`-Step.
Die Projekt-Ebene gilt für jeden dieser Steps, die Step-Ebene ergänzt sie (gleicher
`name` → Step gewinnt), `extra_context: false` am Step schaltet den Kontext dort ab:

    extra_context:
      - {name: agents_md, file: AGENTS.md, trusted: true}
      - {name: quality, cmd: "bash scripts/sonar-scan.sh --pr {pr_number}", trusted: true}
      - {name: issue, value: "{issue_body}"}

Genau eine Quelle pro Eintrag:
- `file`: relativ zur Step-Workdir (`workdir:` am Step, sonst die des Runs), darf
  sie nicht verlassen.
- `cmd`: `sh -c` in der Step-Workdir, nur stdout zählt; Platzhalter nur für
  vertrauenswürdige Werte (wie im shell-Step, ADR 0008). Env wie im shell-Step
  (`process_env()`, ADR 0010): Builder-Identität, kein geerbter Reviewer-Token.
- `value`: Template mit `{variablen}`.

Default ist untrusted: der Inhalt wird als Daten markiert (Prompt-Injection-Schutz,
Leitprinzip 7). `trusted: true` nur für repo-eigene Dateien und Tool-Ausgaben.
Aufgelöst wird bei jedem Prompt-Aufbau, `cmd`-Quellen liefern also z. B. pro
Review-Runde frische Werte. Eine nicht ladbare Quelle bricht den Step nicht ab:
der Block nennt den Grund, das Log warnt.

Budget: jede Quelle wird auf `max_chars` gekürzt (Default `EXTRA_CONTEXT_MAX_CHARS`),
alle Quellen eines Prompts zusammen auf `EXTRA_CONTEXT_TOTAL_MAX_CHARS` (in Spec-
Reihenfolge; was nicht mehr passt, wird gekürzt bzw. ausgelassen, das Log warnt).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..context import _cap, _substitute, prompt_block, step_workdir, untrusted_blocks
from ..gitops import process_env
from ..secrets import _redact
from .shell import run_shell

LLM_STEP_TYPES = frozenset({"llm", "agent", "review", "pr_review", "human_gate"})
SOURCE_KINDS = ("file", "cmd", "value")
DEFAULT_CMD_TIMEOUT_S = 300
# Zeichen je Quelle bzw. für alle Quellen eines Prompts: lokale Modelle (llama.cpp,
# 131k Token geteilt zwischen Slots) laufen sonst bei großen cmd-Ausgaben über.
EXTRA_CONTEXT_MAX_CHARS = 50_000
EXTRA_CONTEXT_TOTAL_MAX_CHARS = 120_000
KEY = "extra_context"
VERIFY_STEP_TYPE = "verify"

_TRUSTED_HEADER = "\n\n## Zusätzlicher Kontext\n"


@dataclass(frozen=True)
class ContextSource:
    """Eine Kontextquelle aus der Spec (siehe Modul-Doku)."""

    name: str
    kind: str  # einer von SOURCE_KINDS
    source: str  # Pfad, Befehl oder Template
    trusted: bool = False
    timeout: int = DEFAULT_CMD_TIMEOUT_S
    max_chars: int = EXTRA_CONTEXT_MAX_CHARS

    @classmethod
    def parse(cls, raw: object) -> ContextSource:
        """Spec-Eintrag → ContextSource; ValueError mit lesbarem Grund bei ungültigem Eintrag."""
        if not isinstance(raw, dict):
            raise ValueError("Eintrag muss ein Mapping sein")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name fehlt")
        kinds = [k for k in SOURCE_KINDS if k in raw]
        if len(kinds) != 1:
            raise ValueError(f"{name}: genau eine Quelle aus {SOURCE_KINDS} nötig")
        kind = kinds[0]
        source = raw[kind]
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"{name}: {kind} muss ein nicht-leerer String sein")
        trusted = raw.get("trusted", False)
        if not isinstance(trusted, bool):
            raise ValueError(f"{name}: trusted muss true/false sein")
        return cls(
            name=name.strip(),
            kind=kind,
            source=source,
            trusted=trusted,
            timeout=_positive_int(raw, "timeout", DEFAULT_CMD_TIMEOUT_S, name),
            max_chars=_positive_int(raw, "max_chars", EXTRA_CONTEXT_MAX_CHARS, name),
        )

    def load(self, ctx: dict, workdir: Path) -> str:
        """Inhalt der Quelle (`file`/`cmd` relativ zu ``workdir``); OSError/ValueError/SubprocessError bei Fehlern."""
        if self.kind == "value":
            return _substitute(self.source, ctx)
        if self.kind == "file":
            return self._file(workdir).read_text(encoding="utf-8")
        p = run_shell(_substitute(self.source, ctx), str(workdir), process_env(), self.timeout)
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or ["kein Output"]
            raise subprocess.SubprocessError(f"exit {p.returncode}: {_redact(tail[0])}")
        return _redact(p.stdout or "")

    def _file(self, workdir: Path) -> Path:
        rel = Path(self.source)
        if rel.is_absolute():
            raise ValueError(f"absoluter Pfad nicht erlaubt: {self.source}")
        root = workdir.resolve()
        path = (root / rel).resolve()
        if path != root and root not in path.parents:
            raise ValueError(f"Pfad verlässt die Workdir: {self.source}")
        return path


def _positive_int(raw: dict, key: str, default: int, name: str) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name}: {key} muss int >= 1 sein")
    return value


def validate(raw: object, where: str, step_type: str | None = None, *, allow_false: bool = True) -> list[str]:
    """Spec-Fehler eines `extra_context`-Werts: Liste von Einträgen, am Step auch `false`.

    `step_type` gesetzt (Step-Ebene) → nur für `LLM_STEP_TYPES` zulässig."""
    if step_type is not None and step_type not in LLM_STEP_TYPES:
        return [f"{where}: extra_context wirkt nur bei {sorted(LLM_STEP_TYPES)}"]
    if raw is False and allow_false:
        return []
    if not isinstance(raw, list):
        return [f"{where}: extra_context muss eine Liste sein" + (" (oder false)" if allow_false else "")]
    errs = []
    for i, entry in enumerate(raw):
        try:
            ContextSource.parse(entry)
        except ValueError as e:
            errs.append(f"{where}: extra_context[{i}]: {e}")
    return errs


def effective_step(spec: dict, step: dict) -> dict:
    """Step mit zusammengeführtem `extra_context` (Projekt + Step); bei `verify` für jeden Check.

    Andere Steps bleiben unverändert."""
    if step.get("type") == VERIFY_STEP_TYPE and isinstance(step.get("checks"), list):
        checks = [effective_step(spec, c) if isinstance(c, dict) else c for c in step["checks"]]
        return {**step, "checks": checks}
    if step.get("type") not in LLM_STEP_TYPES:
        return step
    own = step.get(KEY)
    if own is False:
        return {**step, KEY: []}
    merged = {e["name"]: e for e in (spec.get(KEY) or []) if isinstance(e, dict) and "name" in e}
    merged.update({e["name"]: e for e in (own or []) if isinstance(e, dict) and "name" in e})
    return {**step, KEY: list(merged.values())} if merged else step


def _fit(src: ContextSource, text: str, budget: int, emit) -> str:
    """Kürzt ``text`` auf ``max_chars`` der Quelle und das verbleibende Gesamtbudget (mit Log-Warnung)."""
    if budget <= 0:
        emit(
            f"  ⚠ extra_context {src.name} ausgelassen: Gesamtbudget ({EXTRA_CONTEXT_TOTAL_MAX_CHARS} Zeichen) erschöpft"
        )
        return "[ausgelassen: Gesamtbudget für extra_context erschöpft]"
    limit = min(src.max_chars, budget)
    if len(text) > limit:
        emit(f"  ⚠ extra_context {src.name} gekürzt: {len(text)} → {limit} Zeichen")
    return _cap(text, limit)


def render_extra_context(ctx: dict, step: dict, emit) -> str:
    """Prompt-Anhang aus `step['extra_context']`: trusted als Kontext, sonst als untrusted Daten."""
    trusted: list[tuple[str, str]] = []
    untrusted: list[tuple[str, str]] = []
    workdir = Path(step_workdir(ctx, step))
    budget = EXTRA_CONTEXT_TOTAL_MAX_CHARS
    for raw in step.get(KEY) or []:
        src = ContextSource.parse(raw)
        try:
            text = src.load(ctx, workdir)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            emit(f"  ⚠ extra_context {src.name} nicht verfügbar: {e}")
            text = f"[nicht verfügbar: {e}]"
        text = _fit(src, text.strip(), budget, emit)
        budget -= len(text)
        (trusted if src.trusted else untrusted).append((src.name, text))
    if trusted or untrusted:
        emit(f"  extra_context: {len(trusted)} trusted, {len(untrusted)} untrusted")
    out = ""
    if trusted:
        out += _TRUSTED_HEADER + "\n".join(prompt_block("context", n, t) for n, t in trusted)
    return out + untrusted_blocks(untrusted)
