"""Step: human_gate — Kritizitäts-Check am PR-Diff, Pause bei kritisch (ADR 0010).

Menschliche Freigabe NUR bei kritischen Entscheidungen: ADR-Änderung, großes
Refactoring/Architektur-Change, Datenverlust, GDPR, …. Nicht kritisch →
der Loop läuft autonom weiter (Auto-Merge).

Erkennung (kombiniert, fail-closed):
  - Heuristiken: ADR-Pfade, Diff-Größe (Dateien/Zeilen), Keywords
  - LLM-Meinung: Verdict-JSON {"gate_critical", "gate_reason"}; Provider per
    ``provider:`` pinnen (sonst der aktive Chat-Provider, UI-umschaltbar)
  - Statisch "immer menschlich" bleibt `approval: true` in der Loop-Spec.

Kritisch → Step liefert `needs_approval` → Engine pausiert den Run
(needs_approval). Der ctx-Flag `human_gate_passed` wird vor der Pause
persistiert; beim Resume erkenne ich den Flag und lasse den Run durch,
ohne den Check zu wiederholen.

Step-Def:
    type: human_gate
    repo: owner/name
    pr: {pr_number}        # optional; Default: ctx-Variable pr_number (pr-Step)
    adr_paths: [docs/adr]  # Default: config.HUMAN_GATE_ADR_PATHS
    max_files: 40          # Default: config.HUMAN_GATE_MAX_FILES
    max_lines: 1500        # Default: config.HUMAN_GATE_MAX_LINES
    keywords: [...]        # Default: config.HUMAN_GATE_KEYWORDS
    llm: true              # LLM-Meinung (Default true)
    provider: claude       # optional, wie llm-Step; ebenso model, max_tokens, timeout
"""

from __future__ import annotations

from .. import config
from ..context import _cap
from ..failures import ConfigError, failed, from_result
from ..gitops import _gh_api, _gh_token
from .llm import _step_llm
from .pr_review import _resolve_pr_number

# Step-Keys, die an den llm-Step durchgereicht werden (Provider-Pinning, Budget).
_LLM_PASSTHROUGH_KEYS = ("provider", "model", "max_tokens", "timeout", "extra_context")

_LLM_PROMPT = (
    "Entscheide, ob die folgende PR-Änderung KRITISCH ist und eine menschliche "
    "Freigabe erfordert. Kritisch = ja, wenn: eine ADR-/Architektur-Entscheidung "
    "geändert oder obsolet wird, ein großes Refactoring/Architektur-Change vorliegt, "
    "Datenverlust möglich ist, GDPR/Datenschutz betroffen ist, Sicherheitsrelevanz "
    "(Secrets, Krypto, Auth) besteht, oder die Änderung andere Subsysteme "
    "unvorhersehbar bricht. Ansonsten nein.\n"
    'Antworte NUR mit JSON: {"gate_critical": true|false, "gate_reason": "<ein Satz>"}\n\n'
    "Geänderte Dateien:\n{files}\n\n"
    "Diff (gekürzt):\n---\n{diff}\n---\n"
)


def _heuristic_reasons(files: list[dict], step: dict) -> list[str]:
    """Diff-Heuristiken: ADR-Pfade, Größe, Keywords. Liefert Gründe (leer = ok)."""
    adr_paths = [str(p) for p in (step.get("adr_paths") or config.HUMAN_GATE_ADR_PATHS)]
    max_files = int(step.get("max_files", config.HUMAN_GATE_MAX_FILES))
    max_lines = int(step.get("max_lines", config.HUMAN_GATE_MAX_LINES))
    keywords = [str(k).lower() for k in (step.get("keywords") or config.HUMAN_GATE_KEYWORDS)]
    reasons = []
    for f in files:
        name = str(f.get("filename", ""))
        for adr in adr_paths:
            if name.startswith(adr.rstrip("/") + "/") or name == adr:
                reasons.append(f"ADR-/Entscheidungs-Doku geändert: {name}")
    total_lines = sum(int(f.get("additions", 0)) + int(f.get("deletions", 0)) for f in files)
    if len(files) > max_files:
        reasons.append(f"Diff groß: {len(files)} Dateien (Limit {max_files})")
    if total_lines > max_lines:
        reasons.append(f"Diff groß: {total_lines} Zeilen +/- (Limit {max_lines})")
    for kw in keywords:
        for f in files:
            haystack = (str(f.get("filename", "")) + " " + str(f.get("patch", ""))).lower()
            if kw in haystack:
                reasons.append(f"Keyword '{kw}' in {f.get('filename', '?')}")
                break
    return reasons


def _llm_reason(files: list[dict], diff: str, ctx: dict, step: dict, emit) -> tuple[str | None, dict]:
    """LLM-Meinung. Liefert (grund|None, llm-result). None = nicht kritisch."""
    file_list = "\n".join(
        f"- {f.get('filename', '?')} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})" for f in files[:100]
    )
    prompt = _LLM_PROMPT.replace("{files}", file_list).replace("{diff}", _cap(diff))
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
    # Resume nach menschlicher Freigabe: Flag aus record["vars"] — Check überspringen.
    if str(ctx.get("human_gate_passed", "")) == "true":
        return {"ok": True, "summary": "human_gate: freigegeben (Resume)"}
    repo = str(step.get("repo") or "").strip()
    if not repo:
        raise ConfigError("human_gate: 'repo: owner/name' fehlt")
    token = _gh_token("dev")
    pr_number = _resolve_pr_number(ctx, step, repo, token)
    files = _gh_api("GET", f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files", token)
    if not isinstance(files, list):
        return {"ok": False, "summary": f"human_gate: unerwartete API-Antwort für PR #{pr_number}-Files"}
    diff = "\n".join(str(f.get("patch", "")) for f in files)

    reasons = _heuristic_reasons(files, step)
    usage = None
    if step.get("llm", True) and not reasons:
        llm_reason, res = _llm_reason(files, diff, ctx, step, emit)
        usage = res.get("usage")
        if not res.get("ok"):
            # Fail-closed: ohne LLM-Meinung kein Auto-Merge.
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
            "summary": f"human_gate: PR #{pr_number} nicht kritisch — auto weiter",
            "usage": usage,
        }
    # Kritisch: Flag VOR der Pause setzen (wird per record["vars"] persistiert,
    # damit der Resume-Run den Check nicht wiederholt).
    ctx["human_gate_passed"] = "true"
    what = "Kritische Änderung (menschliche Freigabe): " + " · ".join(reasons)
    emit(f"  ⚠ kritisch: {what}")
    return {
        "ok": True,
        "needs_approval": True,
        "approval": {"reason": "criticality", "what": what},
        "summary": f"human_gate: PR #{pr_number} kritisch — Pause",
        "usage": usage,
    }
