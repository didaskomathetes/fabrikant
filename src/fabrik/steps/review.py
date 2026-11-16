"""Step: review — Cross-Model Cold Critic (ADR 0003, Issue #12).

Startet eine Agent-Session mit **anderem Modell** als der Builder-Step
(frischer Kontext, kein Bias). Der Reviewer-Verdict landet im Run-Record
(`review`-Feld: verdict, findings). `changes_requested` → Step failt →
Run failt (Findings in der Summary). Der Reviewer kann also den Run
zurücksetzen, der Builder nicht.

Step-Def:
    type: review
    model: <Pflicht, muss ≠ Builder-Modell sein>
    backend: opencode | claude_code | codex   (Default: opencode / FABRIK_AGENT_BACKEND)
    endpoint: <opencode>
    target: <optional, {vars}-substituiert; Default: Output des letzten Agent-Steps>
    prompt_file: <optional, ersetzt die Review-Vorlage>
"""

from __future__ import annotations

import json
import os

from ..context import _substitute
from ..failures import failed
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

# Backend der Review-Steps, wenn weder Step noch FABRIK_AGENT_BACKEND eins setzen.
DEFAULT_REVIEW_BACKEND = "opencode"
# So viele Findings nennt die Summary eines gescheiterten Reviews.
SUMMARY_FINDINGS = 5
# Status-Vokabular der AC-Verifikation (ADR 0013 Stufe 2).
ACCEPTANCE_STATUSES = ("met", "unmet", "unverifiable")


def _validate_acceptance(raw: object) -> list[dict] | None:
    """``acceptance``-Feld des Verdicts validieren (ADR 0013 Stufe 2).

    Liefert die normalisierte Liste ``[{criterion, status, evidence}]``;
    fehlendes Feld → ``[]``. Ungültige Struktur (kein List, Eintrag kein
    Dict, leeres Kriterium, unbekannter Status) → ``None`` — das Verdict
    wird abgelehnt statt still ignoriert."""
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
    """JSON-Verdict aus dem Agent-Final-Text extrahieren (tolerant:
    erstes '{' bis letztes '}'). None = kein gültiges Verdict."""
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
        return None  # ungültiges acceptance-JSON wird abgelehnt, nicht still ignoriert (ADR 0013)
    d["acceptance"] = acceptance
    return d


def _resolve_backend(step: dict) -> str:
    """Reviewer-Backend: Step → ``FABRIK_AGENT_BACKEND`` → ``opencode``."""
    return (step.get("backend") or os.environ.get("FABRIK_AGENT_BACKEND") or DEFAULT_REVIEW_BACKEND).strip()


def _check_cold_critic(model: str, ctx: dict, label: str) -> dict | None:
    """Prüft das Reviewer-Modell (ADR 0003): Pflicht und ≠ Builder-Modell.

    ``label`` leitet die Fehlermeldung ein (``review-Step``, ``pr_review``).
    Liefert ein ``failed``-Result oder None, wenn das Modell passt."""
    if not model:
        return failed(f"{label}: model fehlt (Pflicht)", FailureKind.ENVIRONMENT)
    builder_model = (ctx.get("last_agent_model") or "").strip()
    if builder_model and builder_model == model:
        return failed(
            f"{label}: model muss ≠ Builder-Modell sein ({model}) — Cold Critic ist der Punkt",
            FailureKind.ENVIRONMENT,
        )
    return None


def _finding_label(f: dict) -> str:
    """Ein Finding als ``[severity] text`` (fehlende Felder → ``?`` bzw. leer)."""
    return f"[{f.get('severity', '?')}] {f.get('text', '')}"


def _summarize_findings(findings: list[dict], n: int = SUMMARY_FINDINGS) -> str:
    """Die ersten ``n`` Findings einzeilig für die Step-Summary; leer → ``ohne Findings``."""
    return "; ".join(_finding_label(f) for f in findings[:n]) or "ohne Findings"


def _review_field(verdict: dict, model: str, **extra) -> dict:
    """``review``-Feld des Run-Records (Verdict-Vertrag, ADR 0003) plus Step-spezifische Felder.

    ``acceptance`` (ADR 0013): AC-Verifikation pro Kriterium — auch
    `unverifiable` landet im Run-Record, blockiert aber nicht."""
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
        return failed("review-Step: nichts zu reviewen (kein target, kein Agent-Output davor)", FailureKind.ENVIRONMENT)
    target = _substitute(target, ctx)
    if step.get("prompt_file"):
        pf = resolve_relative(step["prompt_file"], ctx.get("spec_path"))
        if not pf.is_file():
            return failed(f"review-Step: prompt_file fehlt: {pf}", FailureKind.ENVIRONMENT)
        prompt = _substitute(pf.read_text(encoding="utf-8").strip(), ctx)
    else:
        prompt = _REVIEW_TEMPLATE.replace("{target}", target)
    prompt += render_extra_context(ctx, step, emit)
    res = _step_agent_backend(ctx, step, emit, _resolve_backend(step), prompt)
    if not res.get("ok"):
        return res  # Backend-Fehler — kein Verdict
    verdict = _parse_verdict(res.get("output", ""))
    if verdict is None:
        return {
            "ok": False,
            "summary": "review-Step: kein JSON-Verdict im Agent-Output",
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
