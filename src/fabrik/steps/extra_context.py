"""Additional prompt context for LLM nodes (`extra_context`).

Applies to all step types in `LLM_STEP_TYPES`, also as a check in a `verify` step.
The project level applies to each of these steps, the step level adds to it (same
`name` → step wins), `extra_context: false` on the step turns the context off there:

    extra_context:
      - {name: agents_md, file: AGENTS.md, trusted: true}
      - {name: quality, cmd: "bash scripts/sonar-scan.sh --pr {pr_number}", trusted: true}
      - {name: issue, value: "{issue_body}"}

Exactly one source per entry:
- `file`: relative to the step workdir (`workdir:` on the step, otherwise the run's), must
  not leave it.
- `cmd`: `sh -c` in the step workdir, only stdout counts; placeholders only for
  trusted values (like in the shell step, ADR 0008). Env like in the shell step
  (`process_env()`, ADR 0010): builder identity, no inherited reviewer token.
- `value`: template with `{variables}`.

Default is untrusted: the content is marked as data (prompt-injection protection,
guiding principle 7). `trusted: true` only for repo-own files and tool outputs.
Resolution happens on every prompt build, so `cmd` sources deliver fresh values e.g. per
review round. A source that cannot be loaded does not abort the step:
the block names the reason, the log warns.

Budget: every source is truncated to `max_chars` (default `EXTRA_CONTEXT_MAX_CHARS`),
all sources of a prompt together to `EXTRA_CONTEXT_TOTAL_MAX_CHARS` (in spec
order; what no longer fits is truncated or omitted, the log warns).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..context import _cap, _substitute, prompt_block, step_workdir, untrusted_blocks
from ..gitops import process_env
from ..i18n import t as _t
from ..secrets import _redact
from .shell import run_shell

LLM_STEP_TYPES = frozenset({"llm", "agent", "review", "pr_review", "human_gate"})
SOURCE_KINDS = ("file", "cmd", "value")
DEFAULT_CMD_TIMEOUT_S = 300
# Characters per source or for all sources of a prompt: local models (llama.cpp,
# 131k tokens shared between slots) would otherwise overflow on large cmd outputs.
EXTRA_CONTEXT_MAX_CHARS = 50_000
EXTRA_CONTEXT_TOTAL_MAX_CHARS = 120_000
KEY = "extra_context"
VERIFY_STEP_TYPE = "verify"


@dataclass(frozen=True)
class ContextSource:
    """A context source from the spec (see module doc)."""

    name: str
    kind: str  # one of SOURCE_KINDS
    source: str  # Pfad, Befehl oder Template
    trusted: bool = False
    timeout: int = DEFAULT_CMD_TIMEOUT_S
    max_chars: int = EXTRA_CONTEXT_MAX_CHARS

    @classmethod
    def parse(cls, raw: object) -> ContextSource:
        """Spec entry → ContextSource; ValueError with a readable reason on an invalid entry."""
        if not isinstance(raw, dict):
            raise ValueError(_t("steps.extra_context.entry_must_be_a_mapping"))
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(_t("steps.extra_context.name_missing"))
        kinds = [k for k in SOURCE_KINDS if k in raw]
        if len(kinds) != 1:
            raise ValueError(_t("steps.extra_context.exactly_one_source_from_required", name=name, v2=SOURCE_KINDS))
        kind = kinds[0]
        source = raw[kind]
        if not isinstance(source, str) or not source.strip():
            raise ValueError(_t("steps.extra_context.must_be_a_non_empty", name=name, kind=kind))
        trusted = raw.get("trusted", False)
        if not isinstance(trusted, bool):
            raise ValueError(_t("steps.extra_context.trusted_must_be_true_false", name=name))
        return cls(
            name=name.strip(),
            kind=kind,
            source=source,
            trusted=trusted,
            timeout=_positive_int(raw, "timeout", DEFAULT_CMD_TIMEOUT_S, name),
            max_chars=_positive_int(raw, "max_chars", EXTRA_CONTEXT_MAX_CHARS, name),
        )

    def load(self, ctx: dict, workdir: Path) -> str:
        """Content of the source (`file`/`cmd` relative to ``workdir``); OSError/ValueError/SubprocessError on errors."""
        if self.kind == "value":
            return _substitute(self.source, ctx)
        if self.kind == "file":
            return self._file(workdir).read_text(encoding="utf-8")
        p = run_shell(_substitute(self.source, ctx), str(workdir), process_env(), self.timeout)
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or [_t("steps.extra_context.no_output")]
            raise subprocess.SubprocessError(f"exit {p.returncode}: {_redact(tail[0])}")
        return _redact(p.stdout or "")

    def _file(self, workdir: Path) -> Path:
        rel = Path(self.source)
        if rel.is_absolute():
            raise ValueError(_t("steps.extra_context.absolute_path_not_allowed", v1=self.source))
        root = workdir.resolve()
        path = (root / rel).resolve()
        if path != root and root not in path.parents:
            raise ValueError(_t("steps.extra_context.path_leaves_the_workdir", v1=self.source))
        return path


def _positive_int(raw: dict, key: str, default: int, name: str) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(_t("steps.extra_context.must_be_int_1", name=name, key=key))
    return value


def validate(raw: object, where: str, step_type: str | None = None, *, allow_false: bool = True) -> list[str]:
    """Spec errors of an `extra_context` value: list of entries, on the step also `false`.

    `step_type` set (step level) → allowed only for `LLM_STEP_TYPES`."""
    if step_type is not None and step_type not in LLM_STEP_TYPES:
        return [_t("steps.extra_context.extra_context_only_applies_to", where=where, v2=sorted(LLM_STEP_TYPES))]
    if raw is False and allow_false:
        return []
    if not isinstance(raw, list):
        return [
            _t("steps.extra_context.extra_context_must_be_a", where=where)
            + (_t("steps.extra_context.or_false") if allow_false else "")
        ]
    errs = []
    for i, entry in enumerate(raw):
        try:
            ContextSource.parse(entry)
        except ValueError as e:
            errs.append(f"{where}: extra_context[{i}]: {e}")
    return errs


def effective_step(spec: dict, step: dict) -> dict:
    """Step with merged `extra_context` (project + step); for `verify` for every check.

    Other steps stay unchanged."""
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
    """Truncates ``text`` to the source's ``max_chars`` and the remaining total budget (with log warning)."""
    if budget <= 0:
        emit(
            _t("steps.extra_context.extra_context_omitted_total_budget", v1=src.name, v2=EXTRA_CONTEXT_TOTAL_MAX_CHARS)
        )
        return _t("steps.extra_context.omitted_total_budget_for_extra")
    limit = min(src.max_chars, budget)
    if len(text) > limit:
        emit(_t("steps.extra_context.extra_context_truncated_characters", v1=src.name, v2=len(text), limit=limit))
    return _cap(text, limit)


def render_extra_context(ctx: dict, step: dict, emit) -> str:
    """Prompt appendix from `step['extra_context']`: trusted as context, otherwise as untrusted data."""
    trusted: list[tuple[str, str]] = []
    untrusted: list[tuple[str, str]] = []
    workdir = Path(step_workdir(ctx, step))
    budget = EXTRA_CONTEXT_TOTAL_MAX_CHARS
    for raw in step.get(KEY) or []:
        src = ContextSource.parse(raw)
        try:
            text = src.load(ctx, workdir)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            emit(_t("steps.extra_context.extra_context_not_available", v1=src.name, e=e))
            text = _t("steps.extra_context.not_available", e=e)
        text = _fit(src, text.strip(), budget, emit)
        budget -= len(text)
        (trusted if src.trusted else untrusted).append((src.name, text))
    if trusted or untrusted:
        emit(f"  extra_context: {len(trusted)} trusted, {len(untrusted)} untrusted")
    out = ""
    if trusted:
        out += _t("extra_context.trusted_header") + "\n".join(prompt_block("context", n, t) for n, t in trusted)
    return out + untrusted_blocks(untrusted)
