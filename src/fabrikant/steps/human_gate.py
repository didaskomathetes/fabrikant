"""Step: human_gate — criticality check on the PR diff, pause if critical (ADR 0010).

Human approval ONLY for critical decisions: ADR change, large
refactoring/architecture change, data loss, GDPR, …. Not critical →
the loop continues autonomously (auto-merge).

Detection (combined, fail-closed):
  - Heuristics: ADR paths, diff size (files/lines), keywords
  - LLM opinion: verdict JSON {"gate_critical", "gate_reason"}; pin the provider via
    ``provider:`` (otherwise the active chat provider, switchable in the UI)
  - Statically "always human" remains `approval: true` in the loop spec.

Critical → the step returns `needs_approval` → the engine pauses the run
(needs_approval). The ctx flag `human_gate_passed` is persisted before the pause;
on resume I recognize the flag and let the run through
without repeating the check.

Step def:
    type: human_gate
    repo: owner/name
    pr: {pr_number}        # optional; default: ctx variable pr_number (pr step)
    adr_paths: [docs/adr]  # default: config.HUMAN_GATE_ADR_PATHS
    max_files: 40          # default: config.HUMAN_GATE_MAX_FILES
    max_lines: 1500        # default: config.HUMAN_GATE_MAX_LINES
    keywords: [...]        # default: config.HUMAN_GATE_KEYWORDS
    llm: true              # LLM opinion (default true)
    provider: claude       # optional, like llm step; likewise model, max_tokens, timeout
"""

from __future__ import annotations

from .. import config
from ..context import _cap
from ..failures import ConfigError, failed, from_result
from ..hosting import FileChange, get_provider
from ..i18n import t
from .llm import _step_llm
from .pr_review import _resolve_pr_number

# Step keys passed on to the llm step (provider pinning, budget).
_LLM_PASSTHROUGH_KEYS = ("provider", "model", "max_tokens", "timeout", "extra_context")


def _heuristic_reasons(files: list[FileChange], step: dict) -> list[str]:
    """Diff heuristics: ADR paths, size, keywords. Returns reasons (empty = ok)."""

    adr_paths = [str(p) for p in (step.get("adr_paths") or config.HUMAN_GATE_ADR_PATHS)]
    max_files = int(step.get("max_files", config.HUMAN_GATE_MAX_FILES))
    max_lines = int(step.get("max_lines", config.HUMAN_GATE_MAX_LINES))
    keywords = [str(k).lower() for k in (step.get("keywords") or config.HUMAN_GATE_KEYWORDS)]
    reasons = []
    for f in files:
        name = f.path
        for adr in adr_paths:
            if name.startswith(adr.rstrip("/") + "/") or name == adr:
                reasons.append(t("human_gate.adr_changed", name=name))
    total_lines = sum(f.additions + f.deletions for f in files)
    if len(files) > max_files:
        reasons.append(t("human_gate.diff_files", n=len(files), limit=max_files))
    if total_lines > max_lines:
        reasons.append(t("human_gate.diff_lines", n=total_lines, limit=max_lines))
    for kw in keywords:
        for f in files:
            haystack = (f.path + " " + f.patch).lower()
            if kw in haystack:
                reasons.append(f"Keyword '{kw}' in {f.path}")
                break
    return reasons


def _llm_reason(files: list[FileChange], diff: str, ctx: dict, step: dict, emit) -> tuple[str | None, dict]:
    """LLM opinion. Returns (reason|None, llm result). None = not critical."""
    file_list = "\n".join(f"- {f.path} (+{f.additions}/-{f.deletions})" for f in files[:100])
    prompt = t("human_gate.llm_prompt").replace("{files}", file_list).replace("{diff}", _cap(diff))
    res = _step_llm(
        ctx,
        {
            "prompt": prompt,
            "schema": {"gate_critical": "boolean", "gate_reason": "string"},
            **{k: step[k] for k in _LLM_PASSTHROUGH_KEYS if k in step},
        },
        emit,
    )
    if not res.get("ok"):
        return None, res
    if ctx.get("gate_critical") is True:
        return str(ctx.get("gate_reason") or "LLM: kritisch"), res
    return None, res


def _step_human_gate(ctx: dict, step: dict, emit) -> dict:
    # Resume after human approval: flag from record["vars"] — skip the check.
    if str(ctx.get("human_gate_passed", "")) == "true":
        return {"ok": True, "summary": "human_gate: freigegeben (Resume)"}
    repo = str(step.get("repo") or "").strip()
    if not repo:
        raise ConfigError(t("steps.human_gate.human_gate_repo_owner_name"))
    provider = get_provider(repo)
    pr_number = _resolve_pr_number(ctx, step, repo, provider)
    files = provider.pr_files(repo, pr_number)
    diff = "\n".join(f.patch for f in files)

    reasons = _heuristic_reasons(files, step)
    usage = None
    if step.get("llm", True) and not reasons:
        llm_reason, res = _llm_reason(files, diff, ctx, step, emit)
        usage = res.get("usage")
        if not res.get("ok"):
            # Fail-closed: without an LLM opinion no auto-merge.
            return failed(
                f"human_gate: LLM-Check fehlgeschlagen (fail-closed): {res.get('summary', '')}",
                from_result(res),
                output=res.get("output", ""),
                usage=usage,
            )
        if llm_reason:
            reasons.append(llm_reason)

    if not reasons:
        return {
            "ok": True,
            "summary": t("steps.human_gate.human_gate_pr_not_critical", pr_number=pr_number),
            "usage": usage,
        }
    # Critical: set the flag BEFORE the pause (persisted via record["vars"],
    # so the resume run does not repeat the check).
    ctx["human_gate_passed"] = "true"
    what = t("human_gate.critical", reasons=" · ".join(reasons))
    emit(f"  ⚠ kritisch: {what}")
    return {
        "ok": True,
        "needs_approval": True,
        "approval": {"reason": "criticality", "what": what},
        "summary": f"human_gate: PR #{pr_number} kritisch — Pause",
        "usage": usage,
    }
