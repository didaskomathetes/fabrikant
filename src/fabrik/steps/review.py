"""Step: review — cross-model cold critic (ADR 0003, issue #12).

Starts an agent session with a **different model** than the builder step
(fresh context, no bias). The reviewer verdict lands in the run record
(`review` field: verdict, findings). `changes_requested` → step fails →
run fails (findings in the summary). So the reviewer can
reset the run, the builder cannot.

Step def:
    type: review
    model: <required, must be ≠ builder model>
    backend: opencode | claude_code | codex   (default: opencode / FABRIK_AGENT_BACKEND)
    endpoint: <opencode>
    target: <optional, {vars}-substituted; default: output of the last agent step>
    prompt_file: <optional, replaces the review template>
"""

from __future__ import annotations

import json
import os

from ..context import _substitute
from ..failures import failed
from ..i18n import t as _t
from ..runmodel import FailureKind
from ..spec_sources import resolve_relative
from .agent import _step_agent_backend
from .extra_context import render_extra_context

_REVIEW_TEMPLATE = (
    "You are an independent reviewer with a fresh context. You were NOT part of the work below.\n"
    "Review it critically and respond with ONLY a JSON object (no markdown, no surrounding text):\n"
    '{"verdict": "approved" | "changes_requested",\n'
    ' "findings": [{"severity": "high" | "medium" | "low", "text": "<finding>"}],\n'
    ' "acceptance": [{"criterion": "<criterion>", "status": "met" | "unmet" | "unverifiable", "evidence": "<evidence>"}],\n'
    ' "summary": "<one sentence>"}\n\n'
    '"acceptance" (ADR 0013) is required only when acceptance criteria were given — one entry\n'
    "per criterion; otherwise omit it or use an empty list.\n\n"
    "Work to review:\n---\n{target}\n---\n"
)

# Backend of the review steps if neither step nor FABRIK_AGENT_BACKEND sets one.
DEFAULT_REVIEW_BACKEND = "opencode"
# This many findings are named in the summary of a failed review.
SUMMARY_FINDINGS = 5
# Status-Vokabular der AC-Verifikation (ADR 0013 Stufe 2).
ACCEPTANCE_STATUSES = ("met", "unmet", "unverifiable")


def _validate_acceptance(raw: object) -> list[dict] | None:
    """Validate the ``acceptance`` field of the verdict (ADR 0013 stage 2).

    Returns the normalized list ``[{criterion, status, evidence}]``;
    missing field → ``[]``. Invalid structure (not a list, entry not a
    dict, empty criterion, unknown status) → ``None`` — the verdict
    is rejected instead of silently ignored."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        criterion = str(entry.get("criterion") or "").strip()
        status = str(entry.get("status") or "").strip()
        if not criterion or status not in ACCEPTANCE_STATUSES:
            return None
        out.append({"criterion": criterion, "status": status, "evidence": str(entry.get("evidence") or "").strip()})
    return out


def _parse_verdict(text: str) -> dict | None:
    """Extract the JSON verdict from the agent final text (tolerant:
    first '{' to last '}'). None = no valid verdict."""
    t = (text or "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        d = json.loads(t[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or d.get("verdict") not in ("approved", "changes_requested"):
        return None
    findings = d.get("findings") or []
    if not isinstance(findings, list):
        findings = [{"severity": "medium", "text": str(findings)}]
    d["findings"] = [f if isinstance(f, dict) else {"severity": "medium", "text": str(f)} for f in findings]
    acceptance = _validate_acceptance(d.get("acceptance"))
    if acceptance is None:
        return None  # invalid acceptance JSON is rejected, not silently ignored (ADR 0013)
    d["acceptance"] = acceptance
    return d


def _resolve_backend(step: dict) -> str:
    """Reviewer-Backend: Step → ``FABRIK_AGENT_BACKEND`` → ``opencode``."""
    return (step.get("backend") or os.environ.get("FABRIK_AGENT_BACKEND") or DEFAULT_REVIEW_BACKEND).strip()


def _check_cold_critic(model: str, ctx: dict, label: str) -> dict | None:
    """Checks the reviewer model (ADR 0003): required and ≠ builder model.

    ``label`` introduces the error message (``review step``, ``pr_review``).
    Returns a ``failed`` result or None if the model fits."""
    if not model:
        return failed(_t("steps.review.model_missing_required", label=label), FailureKind.ENVIRONMENT)
    builder_model = (ctx.get("last_agent_model") or "").strip()
    if builder_model and builder_model == model:
        return failed(
            _t("steps.review.model_must_be_builder_model", label=label, model=model),
            FailureKind.ENVIRONMENT,
        )
    return None


def _finding_label(f: dict) -> str:
    """Ein Finding als ``[severity] text`` (fehlende Felder → ``?`` bzw. leer)."""
    return f"[{f.get('severity', '?')}] {f.get('text', '')}"


def _summarize_findings(findings: list[dict], n: int = SUMMARY_FINDINGS) -> str:
    """The first ``n`` findings on one line for the step summary; empty → ``without findings``."""
    return "; ".join(_finding_label(f) for f in findings[:n]) or _t("steps.review.without_findings")


def _review_field(verdict: dict, model: str, **extra) -> dict:
    """``review`` field of the run record (verdict contract, ADR 0003) plus step-specific fields.

    ``acceptance`` (ADR 0013): AC verification per criterion — even
    `unverifiable` lands in the run record but does not block."""
    return {
        "verdict": verdict["verdict"],
        "model": model,
        "findings": verdict["findings"],
        "acceptance": verdict.get("acceptance", []),
        "summary": str(verdict.get("summary", "")),
        **extra,
    }


def _step_review(ctx: dict, step: dict, emit) -> dict:
    """Cross-Model Cold Critic (ADR 0003)."""
    model = (step.get("model") or "").strip()
    if err := _check_cold_critic(model, ctx, "review-Step"):
        return err
    target = (step.get("target") or "").strip()
    if not target:
        target = (ctx.get("last_agent_output") or "").strip()
    if not target:
        return failed(_t("steps.review.review_step_nothing_to_review"), FailureKind.ENVIRONMENT)
    target = _substitute(target, ctx)
    if step.get("prompt_file"):
        pf = resolve_relative(step["prompt_file"], ctx.get("spec_path"))
        if not pf.is_file():
            return failed(_t("steps.review.review_step_prompt_file_missing", pf=pf), FailureKind.ENVIRONMENT)
        prompt = _substitute(pf.read_text(encoding="utf-8").strip(), ctx)
    else:
        prompt = _REVIEW_TEMPLATE.replace("{target}", target)
    prompt += render_extra_context(ctx, step, emit)
    res = _step_agent_backend(ctx, step, emit, _resolve_backend(step), prompt)
    if not res.get("ok"):
        return res  # Backend error — no verdict
    verdict = _parse_verdict(res.get("output", ""))
    if verdict is None:
        return {
            "ok": False,
            "summary": _t("steps.review.review_step_no_json_verdict"),
            "output": res.get("output", ""),
            "usage": res.get("usage"),
        }
    findings = verdict["findings"]
    review_field = _review_field(verdict, model)
    if verdict["verdict"] == "approved":
        return {
            "ok": True,
            "summary": f"approved · {review_field['summary']}" if review_field["summary"] else "approved",
            "review": review_field,
            "usage": res.get("usage"),
        }
    return failed(
        f"changes_requested ({len(findings)} Findings): {_summarize_findings(findings)}",
        FailureKind.OUTCOME,
        output=res.get("output", ""),
        review=review_field,
        usage=res.get("usage"),
    )
