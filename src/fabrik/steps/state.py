"""Step: state — deterministic, persistent state across runs.

ADR 0001 §4: cron loops (review-pr, pr-reply) keep "already processed"
in JSON files under `state/`. The agent should NOT touch these itself —
the `state` step does get/set/add atomically and emits `state_value` as a variable.

Actions:
- `get`: reads `key`, emits `state_value` (empty if not present).
- `set`: `value` (substitution allowed) replaces `key`.
- `add`: numerically adds to `key` (default 0); non-numeric `value`
  is appended to a list. For round caps / counters.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from ..context import _substitute
from ..filelock import locked
from ..i18n import t

_VALID_ACTIONS = ("get", "set", "add")


def _state_file(ctx: dict, step: dict) -> Path:
    f = step.get("file")
    p = Path(_substitute(str(f), ctx)) if f else Path("state") / f"{ctx.get('project', 'default')}.json"
    if not p.is_absolute():
        p = Path(ctx.get("root", ".")) / p
    return p.resolve()


def _load(p: Path) -> dict:
    with contextlib.suppress(FileNotFoundError):
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    return {}


def _atomic_write(p: Path, data: dict) -> None:
    """tmp write + os.replace: an aborted run leaves nothing half-done."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _to_ctx_val(v: object) -> str | int | float | bool:
    return v if isinstance(v, (str, int, float, bool)) else json.dumps(v, ensure_ascii=False)


def _num(x: object) -> float:
    """Best effort: number (also as a string, e.g. from `set`) → float; otherwise 0."""
    if isinstance(x, bool):
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x)
        except ValueError:
            return 0.0
    return 0.0


def _step_state(ctx: dict, step: dict, emit) -> dict:
    action = str(step.get("action", "")).strip().lower()
    if action not in _VALID_ACTIONS:
        return {
            "ok": False,
            "summary": t(
                "steps.state.state_step_unknown_action_available", action=action, v2=", ".join(_VALID_ACTIONS)
            ),
        }
    key = str(step.get("key", "")).strip()
    if not key:
        return {"ok": False, "summary": t("steps.state.state_step_key_missing")}

    p = _state_file(ctx, step)
    root_res = Path(ctx.get("root", ".")).resolve()
    if root_res not in p.parents:
        return {
            "ok": False,
            "summary": t("steps.state.blocked_state_path_is_outside", p=p, root_res=root_res),
        }

    # Read-modify-write under file lock: parallel runs (ADR 0028) lose no updates.
    with locked(p):
        return _apply(ctx, step, emit, p, action, key)


def _apply(ctx: dict, step: dict, emit, p: Path, action: str, key: str) -> dict:
    data = _load(p)

    if action == "get":
        val = data.get(key)
        ctx["state_value"] = "" if val is None else _to_ctx_val(val)
        emit(f"  state get {key} = {ctx['state_value']!r} ({p})")
        return {"ok": True, "state_value": ctx["state_value"], "summary": f"state {key} = {ctx['state_value']!r}"}

    raw = _substitute(str(step.get("value", "")), ctx)
    if action == "set":
        data[key] = raw
        _atomic_write(p, data)
        ctx["state_value"] = raw
        emit(f"  state set {key} = {raw!r} → {p}")
        return {"ok": True, "state_value": raw, "summary": f"state {key} = {raw!r}"}

    # add: numerisch addieren, sonst an Liste appenden
    cur = data.get(key, 0)
    new: object
    try:
        delta = float(raw)
        base = _num(cur)
        new = base + delta
        new = int(new) if float(new).is_integer() else new
    except ValueError:
        cur = cur if isinstance(cur, list) else []
        cur.append(raw)
        new = cur
    data[key] = new
    _atomic_write(p, data)
    ctx["state_value"] = _to_ctx_val(new)
    emit(f"  state add {key} += {raw!r} → {new!r}")
    return {"ok": True, "state_value": ctx["state_value"], "summary": f"state {key} += {raw!r} → {new!r}"}
