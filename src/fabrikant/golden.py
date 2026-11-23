"""Golden runs + eval harness (Roadmap #130; builds on ADR 0061 replay).

A *golden* pins the stable outcome of a loop (final run status, step statuses
and review verdicts) as a reviewable, committed baseline artifact next to the
spec (``projects/<project>.golden.json``). The *eval* re-executes the loop for
real with the pinned inputs of the baseline run (like
:func:`replay.replay_run`, only with trigger ``eval``) and compares the stable
outcomes with the golden. **Exit 0 = no regression**: "this pipeline change did
not regress my loops" as a checkable statement (CI gate).

Only stable, decision-relevant fields are compared, not volatile LLM text,
duration or token counts. A differing ``spec_hash`` between baseline and eval
is *expected* (a changed pipeline is exactly what gets checked) and therefore
not an error; only the stable-outcome comparison decides about a regression.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, replay
from .filelock import locked
from .runmodel import TERMINAL_RUN_STATUSES, StepStatus

GOLDEN_SCHEMA = 1

# Stable, comparable step fields (decision-relevant, not volatile).
_STEP_FIELDS = ("status", "verdict")

# Step statuses whose outcome is volatile (infrastructure flakes): a baseline
# containing them would pin noise, not behavior.
VOLATILE_STEP_STATUSES = frozenset({StepStatus.TIMEOUT, StepStatus.ERROR})

# Project names end up in a file name -> a single safe path segment only.
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class GoldenError(Exception):
    """Golden file missing/broken or the baseline run is not usable."""


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """Stable outcome of one step."""

    name: str
    status: str = ""
    verdict: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> StepOutcome:
        """Parse a step entry; :class:`GoldenError` if it is not a dict with a string ``name``."""
        if not isinstance(d, dict) or not isinstance(d.get("name"), str):
            raise GoldenError(f"malformed golden step: {d!r}")
        return cls(d["name"], str(d.get("status") or ""), str(d.get("verdict") or ""))

    def to_dict(self) -> dict[str, str]:
        """Serialize to the on-disk shape."""
        return {"name": self.name, "status": self.status, "verdict": self.verdict}


@dataclass(frozen=True, slots=True)
class Golden:
    """A pinned baseline: loop, baseline run and its stable expected outcome."""

    loop: str
    baseline_run: str
    status: str
    steps: tuple[StepOutcome, ...]
    spec_hash: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> Golden:
        """Parse and validate a golden; :class:`GoldenError` if malformed (never a vacuous pass)."""
        if not isinstance(d, dict):
            raise GoldenError(f"malformed golden: expected an object, got {type(d).__name__}")
        for key in ("loop", "baseline_run"):
            if not isinstance(d.get(key), str) or not d[key]:
                raise GoldenError(f"malformed golden: '{key}' must be a non-empty string")
        expect = d.get("expect")
        if not isinstance(expect, dict):
            raise GoldenError("malformed golden: 'expect' must be an object")
        if not isinstance(expect.get("status"), str) or not expect["status"]:
            raise GoldenError("malformed golden: 'expect.status' must be a non-empty string")
        steps = expect.get("steps")
        if not isinstance(steps, list):
            raise GoldenError("malformed golden: 'expect.steps' must be a list")
        spec_hash = d.get("spec_hash", "")
        return cls(
            d["loop"],
            d["baseline_run"],
            expect["status"],
            tuple(StepOutcome.from_dict(s) for s in steps),
            spec_hash if isinstance(spec_hash, str) else "",
        )

    def to_dict(self) -> dict:
        """Serialize to the on-disk shape."""
        return {
            "loop": self.loop,
            "baseline_run": self.baseline_run,
            "spec_hash": self.spec_hash,
            "expect": {"status": self.status, "steps": [s.to_dict() for s in self.steps]},
        }


def golden_path(project: str) -> Path:
    """Path of a project's golden file (next to its spec, ``*.golden.json``).

    Raises :class:`GoldenError` for project names that are not a single safe path segment."""
    if not isinstance(project, str) or not _PROJECT_RE.match(project):
        raise GoldenError(f"invalid project name {project!r}")
    return config.PROJECTS_DIR / f"{project}.golden.json"


def load_goldens(project: str) -> dict[str, dict]:
    """All goldens of a project ``{name: golden}``; ``{}`` if no file exists.

    Raises :class:`GoldenError` for a broken file (invalid JSON, wrong
    ``schema``, malformed golden) -- a half-filled/broken baseline must not
    silently pass as "no goldens" or as a vacuous pass."""
    p = golden_path(project)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise GoldenError(f"golden file {p} not readable: {e}") from e
    if not isinstance(data, dict) or data.get("schema") != GOLDEN_SCHEMA:
        raise GoldenError(f"golden file {p}: expected schema {GOLDEN_SCHEMA}")
    goldens = data.get("goldens", {})
    if not isinstance(goldens, dict):
        raise GoldenError(f"golden file {p}: 'goldens' must be an object")
    for name, g in goldens.items():
        try:
            Golden.from_dict(g)
        except GoldenError as e:
            raise GoldenError(f"golden file {p}, golden {name!r}: {e}") from e
    return goldens


def get_golden(project: str, name: str) -> dict:
    """One golden by name; :class:`GoldenError` if it does not exist."""
    goldens = load_goldens(project)
    if name not in goldens:
        known = ", ".join(sorted(goldens)) or "none"
        raise GoldenError(f"golden '{name}' for project '{project}' unknown (known: {known})")
    return goldens[name]


def stable_outcomes(record: dict) -> dict:
    """Stable, comparable outcome of a run record: ``status`` + per step ``status``/``verdict``.

    This is the surface an eval uses to decide whether a loop regressed --
    deliberately without ``summary`` (often free LLM text), duration and token
    counts, which vary between runs. Steps stay an ordered list (names may repeat)."""
    steps = [
        {"name": s.get("name", ""), "status": s.get("status", ""), "verdict": s.get("verdict", "")}
        for s in record.get("steps") or []
    ]
    return {"status": record.get("status", ""), "steps": steps}


def capture_golden(
    project: str,
    loop: str,
    run_id: str,
    record: dict | None = None,
) -> dict:
    """Build a golden from a finished run (writes nothing yet).

    Passing ``record`` saves a store lookup (the CLI often has it already);
    otherwise it is loaded by id. The baseline must be terminal and free of
    volatile steps (timeout/error) -- such a run would pin noise, not behavior."""
    if record is None:
        from .runs import get_run  # local: runs -> specs -> ... (avoid import cycle)

        record = get_run(run_id, project)
    if record is None:
        raise GoldenError(f"baseline run {run_id} not found (project {project})")
    if record.get("loop") not in (None, loop):
        raise GoldenError(f"baseline run {run_id} belongs to loop {record.get('loop')!r}, not {loop!r}")
    if record.get("status") not in TERMINAL_RUN_STATUSES:
        raise GoldenError(f"baseline run {run_id} not finished (status {record.get('status')!r})")
    volatile = [s.get("name", "") for s in record.get("steps") or [] if s.get("status") in VOLATILE_STEP_STATUSES]
    if volatile:
        raise GoldenError(f"baseline run {run_id} has volatile steps (timeout/error): {', '.join(map(repr, volatile))}")
    return {
        "loop": loop,
        "baseline_run": run_id,
        "spec_hash": record.get("spec_hash", ""),
        "expect": stable_outcomes(record),
    }


def save_golden(project: str, name: str, golden: dict) -> Path:
    """Write a golden into the project file (merge into existing goldens).

    The read-modify-write runs under an exclusive lock (no lost updates between
    concurrent captures); the file is replaced atomically after an fsync."""
    p = golden_path(project)
    Golden.from_dict(golden)  # refuse to persist a malformed golden
    p.parent.mkdir(parents=True, exist_ok=True)
    with locked(p):
        data: dict[str, Any] = {"schema": GOLDEN_SCHEMA, "goldens": load_goldens(project)}
        data["goldens"][name] = golden
        fd = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly below
            "w", encoding="utf-8", dir=p.parent, prefix=p.name + ".", suffix=".tmp", delete=False
        )
        try:
            with fd:
                fd.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                fd.flush()
                os.fsync(fd.fileno())
            os.replace(fd.name, p)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(fd.name)
            raise
    mark_baseline(project, golden.get("baseline_run", ""), name)
    return p


def mark_baseline(project: str, run_id: str, name: str) -> bool:
    """Mark the baseline run in the run store as the golden ``name`` (ADR 0068).

    Best-effort: the committed sidecar file is the source of truth, so a missing
    run (e.g. CI, where the runs dir is empty) or a store error only skips the
    mark and never breaks the capture. Returns ``True`` if the mark was written."""
    if not run_id:
        return False
    try:
        from .runstore import STORE  # local: keep golden importable without the store

        STORE.update_fields(project, run_id, {"golden": name})
        return True
    except Exception:  # noqa: BLE001 - best-effort by design, see docstring
        return False


def golden_for_run(run_id: str) -> tuple[str, str] | None:
    """Resolve a run's golden mark: ``(project, golden_name)`` or ``None``.

    For ``fabrikant eval --golden <run-id>``: reads the run record (any project) and
    returns the project plus the golden name it is the baseline of. ``None`` if
    the run is unknown or not marked as a golden."""
    from .runstore import STORE  # local: keep golden importable without the store

    record = STORE.get_record(run_id, None)
    if record is None:
        return None
    name = record.get("golden", "")
    if not name:
        return None
    return record.get("project", ""), name


def compare_golden(golden: dict, record: dict) -> list[str]:
    """Differences between golden baseline and eval run; ``[]`` = no regression.

    Steps are compared as ordered lists matched by index (like
    :func:`replay.diff_runs`; names may repeat). Any deviation on the stable
    surface counts as a regression: a different step count, a renamed/reordered
    step or a flipped review verdict forces a deliberate re-baseline (capture
    again). A malformed golden raises :class:`GoldenError`."""
    g = Golden.from_dict(golden)
    diffs: list[str] = []
    act_status = record.get("status", "")
    if g.status != act_status:
        diffs.append(f"status: expected {g.status!r}, eval yields {act_status!r}")
    act_steps = record.get("steps") or []
    if len(g.steps) != len(act_steps):
        diffs.append(f"step count: expected {len(g.steps)}, eval yields {len(act_steps)}")
    for i in range(max(len(g.steps), len(act_steps))):
        if i >= len(act_steps):
            diffs.append(f"step {i} {g.steps[i].name!r}: in golden, missing in eval run")
            continue
        a = act_steps[i]
        if i >= len(g.steps):
            diffs.append(f"step {i} {a.get('name', '')!r}: in eval run, missing in golden")
            continue
        exp = g.steps[i]
        if exp.name != a.get("name", ""):
            diffs.append(f"step {i}: expected name {exp.name!r}, eval yields {a.get('name', '')!r}")
        for f in _STEP_FIELDS:
            e, v = getattr(exp, f), a.get(f, "")
            if e != (v or ""):
                diffs.append(f"step {i} {exp.name!r} {f}: expected {e!r}, eval yields {v!r}")
    return diffs


def eval_report(golden: dict, baseline: dict, record: dict, diffs: list[str]) -> str:
    """Human-readable eval report: baseline vs. eval spec hash, status, diffs."""
    gspec = str(golden.get("spec_hash", ""))[:12]
    espec = str(record.get("spec_hash", ""))[:12]
    lines = [
        f"golden {golden.get('loop')!r}: baseline {golden.get('baseline_run')} (spec {gspec}) "
        f"→ eval {record.get('id')} (spec {espec})",
        f"  status: baseline {baseline.get('status', '?')} → eval {record.get('status', '?')}",
    ]
    if diffs:
        lines.append("  REGRESSION:")
        lines.extend(f"    - {d}" for d in diffs)
    else:
        lines.append("  no regression — stable outcomes identical ✔")
    return "\n".join(lines)


def run_eval(
    project: str,
    loop: str,
    name: str | None = None,
    *,
    actor: str | None = None,
    held_scope: str | None = None,
) -> tuple[dict, dict, dict, list[str]]:
    """Load a golden, re-run the loop with baseline inputs (trigger ``eval``), compare.

    Returns ``(golden, baseline_record, eval_record, diffs)``; ``diffs == []`` →
    no regression. The eval run is linked to the baseline run via ``replay_of``
    (ADR 0061) and thus fully traceable. The loop really re-executes -- all
    steps including their side effects (like replay)."""
    name = name or loop
    golden = get_golden(project, name)
    if golden.get("loop") != loop:
        raise GoldenError(f"golden '{name}' belongs to loop {golden.get('loop')!r}, not {loop!r}")
    baseline = replay.get_run(golden["baseline_run"], project)
    if baseline is None:
        raise GoldenError(f"baseline run {golden['baseline_run']!r} not found (any more)")
    _, record = replay.replay_run(
        golden["baseline_run"],
        project=project,
        actor=actor,
        held_scope=held_scope,
        original=baseline,
        trigger=replay.TRIGGER_EVAL,
    )
    diffs = compare_golden(golden, record)
    return golden, baseline, record, diffs
