"""Step: alert_route — deterministisches Routing Triage-Kategorie → Hotfix-Loop (ADR 0045).

Step-Def:
    type: alert_route
    routes: {bug: hotfix}      # Kategorie → Loop desselben Projekts
    fallback: <optional>       # Loop für ungemappte Kategorien; Default: kein Hotfix

Liest `{category}` (vom vorherigen `llm`-Step, enum-validiert), keine LLM-/Tool-Nutzung.
Gleiche Kategorie → gleicher Loop. Der Hotfix läuft als eigener Run (eigener Record,
eigene Tier-/Approval-Gates) und bekommt die `alert_*`-Variablen + `category`."""

from __future__ import annotations

from ..alerts import CATEGORY_UNKNOWN, ROUTE_NONE, VAR_PREFIX, route

# Terminal-Status des Hotfix-Runs, die den Triage-Run nicht scheitern lassen
# (needs_approval = Hotfix wartet auf Freigabe, das ist der gewollte Normalfall).
_OK_STATUSES = ("pass", "needs_approval")


def _step_alert_route(ctx: dict, step: dict, emit) -> dict:
    # Lazy-Import: engine importiert steps bei Modul-Load (zirkulär).
    from ..engine import run_loop

    category = str(ctx.get("category") or CATEGORY_UNKNOWN)
    target = route(category, step.get("routes"), str(step.get("fallback") or ROUTE_NONE))
    ctx["route"] = target
    if target == ROUTE_NONE:
        emit(f"  alert_route: Kategorie '{category}' → kein Hotfix")
        return {"ok": True, "summary": f"category={category}: kein Hotfix-Workflow"}
    if target == ctx.get("loop"):
        return {"ok": False, "summary": f"Route '{target}' zeigt auf den Triage-Loop selbst (Endlosschleife)"}
    initial = {k: v for k, v in ctx.items() if k.startswith(VAR_PREFIX)} | {"category": category}
    emit(f"  alert_route: Kategorie '{category}' → Loop '{target}'")
    rec = run_loop(ctx["project"], target, trigger=f"alert:{ctx['run_id']}", initial_vars=initial)
    ctx["hotfix_run_id"] = rec["id"]
    ctx["hotfix_status"] = rec["status"]
    return {
        "ok": rec["status"] in _OK_STATUSES,
        "summary": f"category={category} → {target}: Run {rec['id']} {rec['status']}",
    }
