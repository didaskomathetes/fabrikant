"""Step: verify — Check-Liste-Semantik (Issue #13).

Eine `verify`-Step ist eine Liste von Checks (`type: shell|http|...`), die
alle grün sein müssen. Jeder Check ist ein normales Step-Def und wird über
das gewöhnliche Step-Dispatch ausgeführt. Bewusst nicht "deterministisch-only"
— LLM-Judges und Screenshot-Diffs können später als Check-Typen dazukommen,
ohne die Semantik zu forken.

Semantik:
- Alle Checks werden durchlaufen (Checkliste, kein Fail-Fast), pro Check wird
  Output gesammelt.
- Alle grün → `pass`; mindestens einer rot → `fail` mit Check-Name + Output.
- Leere/fehlende `checks` → vacuous `pass` (mit Hinweis im Summary).

Der pro-Check-Output landet im Run-Record unter dem Key `verify`.
"""

from __future__ import annotations

from ..context import _cap
from ..failures import Failure, classify_exception, failed, from_result
from ..runmodel import FailureKind


def _step_verify(ctx: dict, step: dict, emit) -> dict:
    # Lazy-Import: engine importiert steps bei Modul-Load; hier nur zur Laufzeit,
    # um den zirkulären Import (engine → steps → engine) zu vermeiden.
    from ..engine import _dispatch_step

    checks = step.get("checks") or []
    if not checks:
        emit("  verify: keine Checks definiert (leere Liste) → vacuous pass")
        return {"ok": True, "verify": [], "summary": "verify: keine Checks definiert (leere Liste) → pass"}

    results: list[dict] = []
    kinds: list[Failure] = []  # Klassen der roten Checks (ADR 0035); unklassifiziert rot → outcome
    for i, chk in enumerate(checks):
        if not isinstance(chk, dict) or "type" not in chk:
            name = str(chk.get("name")) if isinstance(chk, dict) and chk.get("name") else f"check-{i + 1}"
            results.append({"name": name, "ok": False, "output": "ungültige Check-Def (fehlt 'type')"})
            kinds.append(Failure(FailureKind.ENVIRONMENT))
            emit(f"  verify [{name}] → ungültige Def")
            continue

        name = str(chk.get("name") or f"check-{i + 1}")
        emit(f"  verify [{name}] ({chk['type']})")
        try:
            res = _dispatch_step(ctx, dict(chk), emit)
        except Exception as e:  # noqa: BLE001 — ein Check-Fehler darf den Loop nicht crashen
            res = {"ok": False, "output": f"Exception: {e}", "summary": f"Exception: {e}"}
            kinds.append(classify_exception(e))  # unklassifizierte Exception bleibt unknown (ADR 0035 Nr. 4)
        else:
            if not (isinstance(res, dict) and res.get("ok")):
                kinds.append(_check_failure(res))
        ok = bool(res.get("ok")) if isinstance(res, dict) else False
        output = (res.get("output") or res.get("summary") or "") if isinstance(res, dict) else str(res)
        results.append({"name": name, "ok": ok, "output": _cap(str(output))})
        emit(f"    → {'OK' if ok else 'FAIL'}")

    red = [r for r in results if not r["ok"]]
    if red:
        detail = "; ".join(f"{r['name']}: {str(r['output'])[:200]}" for r in red)
        return failed(
            f"verify FAILED ({len(red)}/{len(results)} rot): {detail}", _verify_failure(kinds), verify=results
        )
    return {
        "ok": True,
        "verify": results,
        "summary": f"verify pass ({len(results)}/{len(results)} Checks grün)",
    }


# Welche Klasse eines roten Checks den verify-Step bestimmt: was einen Menschen braucht vor dem, was ein
# Retry heilen könnte, vor einem echten Ergebnis; unknown zuletzt. Muss jede FailureKind enthalten.
_KIND_PRIORITY = (
    FailureKind.ENVIRONMENT,
    FailureKind.INTERRUPTED,
    FailureKind.TRANSIENT,
    FailureKind.OUTCOME,
    FailureKind.UNKNOWN,
)


def _check_failure(res: object) -> Failure:
    """Klasse eines regulär rot gemeldeten Checks; ohne Angabe ein unerfülltes Kriterium → ``outcome``."""
    reported = from_result(res) if isinstance(res, dict) else Failure()
    return Failure(FailureKind.OUTCOME) if reported.kind is FailureKind.UNKNOWN else reported


def _verify_failure(kinds: list[Failure]) -> Failure:
    """Klasse eines roten verify-Steps: die vorrangigste Klasse der roten Checks (:data:`_KIND_PRIORITY`)."""
    return min(kinds, key=lambda f: _KIND_PRIORITY.index(f.kind), default=Failure())
