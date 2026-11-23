"""Step: alert_route — deterministic routing triage category → hotfix loop (ADR 0045).

Step def:
    type: alert_route
    routes: {bug: hotfix}      # category → loop of the same project
    fallback: <optional>       # loop for unmapped categories; default: no hotfix

Reads `{category}` (from the previous `llm` step, enum-validated), no LLM/tool use.
Same category → same loop. The hotfix runs as its own run (own record,
own tier/approval gates) and gets the `alert_*` variables + `category`."""

from __future__ import annotations

from ..alerts import CATEGORY_UNKNOWN, ROUTE_NONE, VAR_PREFIX, route
from ..i18n import t

# Terminal statuses of the hotfix run that do not make the triage run fail
# (needs_approval = hotfix waits for approval, that is the intended normal case).
_OK_STATUSES = ("pass", "needs_approval")


def _step_alert_route(ctx: dict, step: dict, emit) -> dict:
    # Lazy import: engine imports steps at module load (circular).
    from ..engine import run_loop

    category = str(ctx.get("category") or CATEGORY_UNKNOWN)
    target = route(category, step.get("routes"), str(step.get("fallback") or ROUTE_NONE))
    ctx["route"] = target
    if target == ROUTE_NONE:
        emit(t("steps.alert_route.alert_route_category_no_hotfix", category=category))
        return {"ok": True, "summary": t("steps.alert_route.category_no_hotfix_workflow", category=category)}
    if target == ctx.get("loop"):
        return {"ok": False, "summary": t("steps.alert_route.route_points_to_the_triage", target=target)}
    initial = {k: v for k, v in ctx.items() if k.startswith(VAR_PREFIX)} | {"category": category}
    emit(f"  alert_route: Kategorie '{category}' → Loop '{target}'")
    rec = run_loop(ctx["project"], target, trigger=f"alert:{ctx['run_id']}", initial_vars=initial)
    ctx["hotfix_run_id"] = rec["id"]
    ctx["hotfix_status"] = rec["status"]
    return {
        "ok": rec["status"] in _OK_STATUSES,
        "summary": f"category={category} → {target}: Run {rec['id']} {rec['status']}",
    }
