"""Golden Runs + Eval-Harness (Roadmap #130; auf ADR 0061 Replay aufbauend).

Ein *Golden* pinned das stabile Outcome eines Loops — finaler Run-Status,
Step-Status und Review-Verdicts — als reviewbares, committetes Baseline-
Artefakt neben der Spec (``projects/<projekt>.golden.json``). Der *Eval* führt
den Loop mit den gepinnten Inputs des Baseline-Runs real neu aus (wie
:func:`replay.replay_run`, nur Trigger ``eval``) und vergleicht die stabilen
Outcomes mit dem Golden. **Exit 0 = kein Regression**: "diese Pipeline-Änderung
hat meine Loops nicht regressiert" als prüfbare Aussage (CI-Gate).

Verglichen werden nur stabile, entscheidungsrelevante Felder — nicht volatiles
LLM-Text, Laufdauer oder Token-Zahlen. Ein abweichendes ``spec_hash`` zwischen
Baseline und Eval ist *erwartet* (gerade eine geänderte Pipeline wird geprüft)
und daher kein Fehler; nur der Stable-Outcome-Vergleich entscheidet über
Regression.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config, replay
from .runmodel import TERMINAL_RUN_STATUSES

GOLDEN_SCHEMA = 1

# Stabile, vergleichbare Step-Felder (entscheidungsrelevant, nicht volatil).
_STEP_FIELDS = ("status", "verdict")


class GoldenError(Exception):
    """Golden-Datei fehlt/defekt oder der Baseline-Run ist nicht verwertbar."""


def golden_path(project: str) -> Path:
    """Pfad der Golden-Datei eines Projekts (neben dessen Spec, ``*.golden.json``)."""
    return config.PROJECTS_DIR / f"{project}.golden.json"


def load_goldens(project: str) -> dict[str, dict]:
    """Alle Goldens eines Projekts ``{name: golden}``; ``{}`` wenn keine Datei existiert.

    Wirft :class:`GoldenError` bei defekter Datei (ungültiges JSON, falsches
    ``schema``) — eine halbbefüllte/defekte Baseline darf nicht stillschweigend
    als "keine Goldens" durchgehen."""
    p = golden_path(project)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise GoldenError(f"Golden-Datei {p} nicht lesbar: {e}") from e
    if not isinstance(data, dict) or data.get("schema") != GOLDEN_SCHEMA:
        raise GoldenError(f"Golden-Datei {p}: erwartetes schema {GOLDEN_SCHEMA}")
    goldens = data.get("goldens", {})
    if not isinstance(goldens, dict):
        raise GoldenError(f"Golden-Datei {p}: 'goldens' muss ein Objekt sein")
    return goldens


def get_golden(project: str, name: str) -> dict:
    """Ein Golden per Name; :class:`GoldenError`, wenn es nicht existiert."""
    goldens = load_goldens(project)
    if name not in goldens:
        known = ", ".join(sorted(goldens)) or "keine"
        raise GoldenError(f"Golden '{name}' für Projekt '{project}' unbekannt (bekannt: {known})")
    return goldens[name]


def stable_outcomes(record: dict) -> dict:
    """Stabiles, vergleichbares Outcome eines Run-Records: ``status`` + je Step ``status``/``verdict``.

    Das ist die Fläche, über die ein Eval entscheidet, ob sich ein Loop
    regressiert hat — bewusst ohne ``summary`` (oft freies LLM-Text), Dauer und
    Token-Zahlen, die zwischen Runs variieren."""
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
    """Golden aus einem abgeschlossenen Run bauen (schreibt noch nichts).

    ``record`` vorzugeben spart einen Store-Lookup (CLI hat ihn oft schon);
    sonst wird er per Id geladen. Baseline muss terminal sein — ein Golden aus
    einem laufenden/verworfenen Run wäre kein stabiles Baseline."""
    if record is None:
        from .runs import get_run  # lokal: runs → specs → … (Import-Zyklus vermeiden)

        record = get_run(run_id, project)
    if record is None:
        raise GoldenError(f"Baseline-Run {run_id} nicht gefunden (Projekt {project})")
    if record.get("loop") not in (None, loop):
        raise GoldenError(f"Baseline-Run {run_id} gehört zu Loop {record.get('loop')!r}, nicht {loop!r}")
    if record.get("status") not in TERMINAL_RUN_STATUSES:
        raise GoldenError(f"Baseline-Run {run_id} nicht abgeschlossen (Status {record.get('status')!r})")
    return {
        "loop": loop,
        "baseline_run": run_id,
        "spec_hash": record.get("spec_hash", ""),
        "expect": stable_outcomes(record),
    }


def save_golden(project: str, name: str, golden: dict) -> Path:
    """Golden in die Projekt-Datei schreiben (Merge in bestehende Goldens, atomar)."""
    p = golden_path(project)
    data: dict[str, Any] = {"schema": GOLDEN_SCHEMA, "goldens": load_goldens(project)}
    data["goldens"][name] = golden
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


def compare_golden(golden: dict, record: dict) -> list[str]:
    """Differenzen zwischen Golden-Baseline und Eval-Run; ``[]`` = kein Regression.

    Jede Abweichung auf der stabilen Fläche zählt als Regression: ein Step, der
    neu auftritt (Spec-Änderung), ein fehlender Step oder ein umgekipptes
    Review-Verdict zwingt zu einer bewussten Re-Baseline (erneutes Capture)."""
    expect = golden.get("expect", {})
    diffs: list[str] = []
    exp_status = expect.get("status")
    act_status = record.get("status", "")
    if exp_status is not None and exp_status != act_status:
        diffs.append(f"status: erwartet {exp_status!r}, eval liefert {act_status!r}")
    exp_steps = {s.get("name"): s for s in expect.get("steps", [])}
    act_steps = {s.get("name"): s for s in record.get("steps") or []}
    for name, es in exp_steps.items():
        a = act_steps.get(name)
        if a is None:
            diffs.append(f"step {name!r}: im Golden, fehlt im Eval-Run")
            continue
        for f in _STEP_FIELDS:
            e, v = es.get(f, ""), a.get(f, "")
            if (e or "") != (v or ""):
                diffs.append(f"step {name!r} {f}: erwartet {e!r}, eval liefert {v!r}")
    for name in act_steps:
        if name not in exp_steps:
            diffs.append(f"step {name!r}: im Eval-Run, fehlt im Golden")
    return diffs


def eval_report(golden: dict, baseline: dict, record: dict, diffs: list[str]) -> str:
    """Menschlich lesbarer Eval-Bericht: Baseline- vs. Eval-Spec-Hash, Status, Diffs."""
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
        lines.append("  kein Regression — stabile Outcomes identisch ✔")
    return "\n".join(lines)


def run_eval(
    project: str,
    loop: str,
    name: str | None = None,
    *,
    actor: str | None = None,
    held_scope: str | None = None,
) -> tuple[dict, dict, dict, list[str]]:
    """Golden laden, Loop mit Baseline-Inputs neu ausführen (Trigger ``eval``), vergleichen.

    Liefert ``(golden, baseline_record, eval_record, diffs)``; ``diffs == []`` →
    kein Regression. Der Eval-Run ist über ``replay_of`` an den Baseline-Run
    verlinkt (ADR 0061) und damit voll nachvollziehbar. Der Loop wird real neu
    ausgeführt — alle Steps inklusive ihrer Seiteneffekte (wie Replay)."""
    name = name or loop
    golden = get_golden(project, name)
    if golden.get("loop") != loop:
        raise GoldenError(f"Golden '{name}' gehört zu Loop {golden.get('loop')!r}, nicht {loop!r}")
    baseline = replay.get_run(golden["baseline_run"], project)
    if baseline is None:
        raise GoldenError(f"Baseline-Run {golden['baseline_run']!r} nicht (mehr) gefunden")
    _, record = replay.replay_run(
        golden["baseline_run"],
        project=project,
        actor=actor,
        held_scope=held_scope,
        original=baseline,
        trigger="eval",
    )
    diffs = compare_golden(golden, record)
    return golden, baseline, record, diffs
