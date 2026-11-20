"""Replay: deterministic re-execution of a finished run (ADR 0061, Issue #129).

`replay_run` re-executes a finished run with **pinned inputs** — the original
spec (checked via `spec_hash`), the original run variables, loop, trigger and
note — and links the new run to its original via the `replay_of` record field.
`diff_runs` renders a text diff report (status, steps, verdicts, spec hash).

A replay is *input-deterministic*: the same spec + vars + loop execute the same
steps in the same order with the same inputs. Where outputs diverge (LLM/agent
steps, external state) is exactly what the diff report surfaces.
"""

from __future__ import annotations

from .engine import run_loop
from .runmodel import TERMINAL_RUN_STATUSES
from .runs import get_run
from .specs import load_projects

# Engine-derived context keys (see `engine._build_ctx`) that must NOT be replayed:
# they are re-derived for the new run (fresh workdir/run_id/branch/…) and would
# otherwise clobber them, because `initial_vars` are applied last in `_build_ctx`.
_DERIVED_KEYS = frozenset(
    {
        "project",
        "root",
        "spec_path",
        "workdir",
        "loop",
        "trigger",
        "note",
        "run_id",
        "fabrik_home",
        "branch",
        "head",
    }
)


class ReplayError(Exception):
    """A replay is impossible (unknown run, non-terminal status, missing spec)."""


def _pinned_vars(record: dict, spec: dict) -> dict:
    """Pinned input vars for a replay: the original run's `vars` minus the
    engine-derived keys (workdir, run_id, branch, …) and the spec's `env` keys
    (re-derived from the current spec). What remains are the trigger variables
    and any values the original run's steps emitted into the context."""
    env_keys = set((spec.get("env") or {}).keys())
    return {k: v for k, v in (record.get("vars") or {}).items() if k not in _DERIVED_KEYS and k not in env_keys}


def replay_run(
    run_id: str,
    project: str | None = None,
    actor: str | None = None,
    held_scope: str | None = None,
) -> tuple[dict, dict]:
    """Re-execute a finished run with pinned inputs.

    Returns ``(original_record, replay_record)``. Raises :class:`ReplayError`
    if the run is unknown, not in a terminal status, or its project/spec is gone.
    A spec-hash mismatch is *not* an error — it is reported by :func:`diff_runs`.
    """
    orig = get_run(run_id, project)
    if orig is None:
        raise ReplayError(f"Run {run_id} not found")
    status = orig.get("status")
    if status not in TERMINAL_RUN_STATUSES:
        raise ReplayError(f"Run {run_id} is '{status}', not terminal — replay only works on finished runs")
    proj = orig["project"]
    loop = orig["loop"]
    spec = load_projects().get(proj)
    if not spec or spec.get("_error"):
        raise ReplayError(f"Project '{proj}' (spec for run {run_id}) is not loadable")
    if loop not in (spec.get("loops") or {}):
        raise ReplayError(f"Loop '{loop}' of run {run_id} no longer exists in the spec")

    rec = run_loop(
        proj,
        loop,
        trigger="replay",
        note=f"replay of {run_id}",
        initial_vars=_pinned_vars(orig, spec) or None,
        actor=actor,
        held_scope=held_scope,
        replay_of=run_id,
    )
    return orig, rec


def diff_runs(original: dict, replay: dict) -> str:
    """Text diff report of original vs. replay: spec hash, status, per-step
    status/summary/verdict (matched by index — step order is pinned by the spec)."""
    lines: list[str] = []
    lines.append(f"replay diff: {original.get('id', '?')} → {replay.get('id', '?')}")

    oh, rh = original.get("spec_hash", ""), replay.get("spec_hash", "")
    lines.append(
        f"spec_hash: {oh or '?'} {'==' if oh == rh else '!='} {rh or '?'}  ({'match' if oh == rh else 'MISMATCH'})"
    )

    os_, rs = original.get("status"), replay.get("status")
    lines.append(f"status: {os_} {'==' if os_ == rs else '!='} {rs}")
    if original.get("failure_kind"):
        lines.append(f"failure_kind (original): {original['failure_kind']}")
    if replay.get("failure_kind"):
        lines.append(f"failure_kind (replay): {replay['failure_kind']}")

    osteps = original.get("steps") or []
    rsteps = replay.get("steps") or []
    for i in range(max(len(osteps), len(rsteps))):
        o = osteps[i] if i < len(osteps) else None
        r = rsteps[i] if i < len(rsteps) else None
        if o is None:
            assert r is not None  # i < max(len) ⇒ the other side exists
            lines.append(f"step {i}: <missing in original>  {r['name']} [{r['type']}] {r['status']}")
            continue
        if r is None:
            lines.append(f"step {i}: {o['name']} [{o['type']}] {o['status']}  <missing in replay>")
            continue
        same = (
            o.get("status") == r.get("status")
            and o.get("summary", "") == r.get("summary", "")
            and o.get("verdict", "") == r.get("verdict", "")
        )
        lines.append(
            f"step {i} {o['name']} [{o['type']}]: {o.get('status')} {'==' if same else '!='} {r.get('status')}"
        )
        if o.get("summary", "") != r.get("summary", ""):
            lines.append(f"    summary: {o.get('summary', '')!r} != {r.get('summary', '')!r}")
        if o.get("verdict", "") != r.get("verdict", ""):
            lines.append(f"    verdict: {o.get('verdict', '')!r} != {r.get('verdict', '')!r}")
    return "\n".join(lines)
