"""Step: verify — check-list semantics (issue #13).

A `verify` step is a list of checks (`type: shell|http|...`) that must
all be green. Each check is a normal step def and is executed via
the ordinary step dispatch. Deliberately not "deterministic-only"
— LLM judges and screenshot diffs can be added later as check types
without forking the semantics.

Semantics:
- All checks are run (checklist, no fail-fast), output is collected per check.
- All green → `pass`; at least one red → `fail` with check name + output.
- Empty/missing `checks` → vacuous `pass` (with a note in the summary).

The per-check output lands in the run record under the key `verify`.
"""

from __future__ import annotations

from ..context import _cap
from ..failures import Failure, classify_exception, failed, from_result
from ..i18n import t
from ..runmodel import FailureKind


def _step_verify(ctx: dict, step: dict, emit) -> dict:
    # Lazy import: engine imports steps at module load; here only at runtime,
    # to avoid the circular import (engine → steps → engine).
    from ..engine import _dispatch_step

    checks = step.get("checks") or []
    if not checks:
        emit(t("steps.verify.verify_no_checks_defined_empty"))
        return {"ok": True, "verify": [], "summary": t("steps.verify.verify_no_checks_defined_empty_2")}

    results: list[dict] = []
    kinds: list[Failure] = []  # classes of the failing checks (ADR 0035); unclassified red → outcome
    for i, chk in enumerate(checks):
        if not isinstance(chk, dict) or "type" not in chk:
            name = str(chk.get("name")) if isinstance(chk, dict) and chk.get("name") else f"check-{i + 1}"
            results.append(
                {"name": name, "ok": False, "output": t("steps.verify.invalid_check_definition_type_missing")}
            )
            kinds.append(Failure(FailureKind.ENVIRONMENT))
            emit(t("steps.verify.verify_invalid_definition", name=name))
            continue

        name = str(chk.get("name") or f"check-{i + 1}")
        emit(f"  verify [{name}] ({chk['type']})")
        try:
            res = _dispatch_step(ctx, dict(chk), emit)
        except Exception as e:  # noqa: BLE001 — a check error must not crash the loop
            res = {"ok": False, "output": f"Exception: {e}", "summary": f"Exception: {e}"}
            kinds.append(classify_exception(e))  # unclassified exceptions stay unknown (ADR 0035 item 4)
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
        "summary": t("steps.verify.verify_pass_checks_green", v1=len(results)),
    }


# Which class of a red check determines the verify step: what needs a human before what a
# retry could heal, before a real result; unknown last. Must contain every FailureKind.
_KIND_PRIORITY = (
    FailureKind.ENVIRONMENT,
    FailureKind.INTERRUPTED,
    FailureKind.TRANSIENT,
    FailureKind.OUTCOME,
    FailureKind.UNKNOWN,
)


def _check_failure(res: object) -> Failure:
    """Class of a regularly reported red check; without specification an unmet criterion → ``outcome``."""
    reported = from_result(res) if isinstance(res, dict) else Failure()
    return Failure(FailureKind.OUTCOME) if reported.kind is FailureKind.UNKNOWN else reported


def _verify_failure(kinds: list[Failure]) -> Failure:
    """Class of a red verify step: the highest-priority class of the red checks (:data:`_KIND_PRIORITY`)."""
    return min(kinds, key=lambda f: _KIND_PRIORITY.index(f.kind), default=Failure())
