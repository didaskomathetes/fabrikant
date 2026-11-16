"""Spec-Speicherung (save_spec): Round-Trip-Write mit Kommentar-Erhaltung,
Validierung VOR dem Write, atomarer Replace, SpecUpdate-Payload-Parsing.

Die Test-Spec ist bewusst kommentiert — der Kern-Vertrag von save_spec ist,
dass unangetastete Teile (Kommentare, Reihenfolge, andere Loops) überleben.
"""

from __future__ import annotations

import json

import pytest

from fabrik import specs

# Top-Kommentar, Inline-Kommentar und Loops-Kommentar müssen nach einem
# Save überleben; der Kommentar über `l1:` liegt IN der loops-Sektion und
# wird mit deren Ersetzung erwartungsgemäß verworfen.
SPEC_YAML = """\
# Top-Kommentar (muss überleben)
name: p
description: alte Beschreibung
root: __ROOT__
notify:
  github_issue: true  # Inline-Kommentar (muss überleben)
max_parallel: 1
# Loops-Sektion (Kommentar muss überleben)
loops:
  # l1-Kommentar (liegt in der loops-Sektion)
  l1:
    description: loop eins
    triggers:
      - {type: manual}
    steps:
      - {type: shell, name: a, cmd: "echo a"}
  l2:
    triggers:
      - {type: cron}
    steps:
      - {type: shell, name: b, cmd: "echo b"}
"""


@pytest.fixture
def spec_file(projects_dir, tmp_path):
    f = projects_dir / "p.fabrik.yaml"
    f.write_text(SPEC_YAML.replace("__ROOT__", str(tmp_path)))
    return f


def _all_loops(l1: dict | None = None, l2: dict | None = None) -> dict:
    """Beide Loops (Default: unverändert) — PUT mergt pro Loop (unbekannte Schlüssel erhalten)."""
    return {
        "l1": l1
        or {
            "description": "loop eins",
            "triggers": [{"type": "manual"}],
            "steps": [{"type": "shell", "name": "a", "cmd": "echo a"}],
        },
        "l2": l2 or {"triggers": [{"type": "cron"}], "steps": [{"type": "shell", "name": "b", "cmd": "echo b"}]},
    }


# ---------------------------------------------------------------- save_spec (YAML)
def test_save_spec_replaces_loop_and_preserves_comments(spec_file):
    update = specs.SpecUpdate(
        loops=_all_loops(
            l1={
                "description": "neu",
                "triggers": [{"type": "manual"}],
                "steps": [{"type": "shell", "name": "a2", "cmd": "echo a2"}],
            }
        )
    )
    saved = specs.save_spec("p", update)
    assert saved["loops"]["l1"]["description"] == "neu"
    assert saved["_path"] == str(spec_file)
    text = spec_file.read_text()
    # Kommentare der unangetasteten Teile überleben den Round-Trip-Write.
    assert "# Top-Kommentar (muss überleben)" in text
    assert "github_issue: true  # Inline-Kommentar (muss überleben)" in text
    assert "# Loops-Sektion (Kommentar muss überleben)" in text
    # Die übrige Spec (notify, l2) bleibt erhalten.
    assert "notify:" in text
    assert "l2:" in text
    # Die Datei ist über den normalen Pfad wieder ladbar (atomarer Write ok).
    reloaded = specs.load_spec(spec_file)
    assert reloaded["loops"]["l1"]["steps"][0]["cmd"] == "echo a2"
    assert reloaded["loops"]["l2"]["steps"][0]["cmd"] == "echo b"


def test_save_spec_updates_project_fields_only(spec_file):
    update = specs.SpecUpdate(project_fields={"description": "neu", "max_parallel": 4})
    saved = specs.save_spec("p", update)
    assert saved["description"] == "neu"
    assert saved["max_parallel"] == 4
    text = spec_file.read_text()
    assert "description: neu" in text
    assert "max_parallel: 4" in text
    # Loops + Kommentar unverändert.
    assert "l1:" in text and "l2:" in text
    assert "# Top-Kommentar (muss überleben)" in text


def test_save_spec_validation_error_does_not_write(spec_file):
    before = spec_file.read_text()
    update = specs.SpecUpdate(loops=_all_loops(l1={"steps": [{"type": "shell"}]}))  # shell ohne cmd
    with pytest.raises(ValueError, match="shell ohne cmd"):
        specs.save_spec("p", update)
    assert spec_file.read_text() == before  # Datei unangetastet
    assert not list(spec_file.parent.glob("*.tmp"))  # keine Temp-Leiche (NamedTemporaryFile)


def test_save_spec_unknown_project(projects_dir):
    with pytest.raises(KeyError, match="unbekanntes Projekt"):
        specs.save_spec("keiner", specs.SpecUpdate(project_fields={"description": "x"}))


def test_save_spec_broken_spec_rejected(projects_dir, tmp_path):
    (projects_dir / "broken.fabrik.yaml").write_text(
        f"root: {tmp_path}\nloops:\n  l1:\n    steps:\n      - type: bogus\n"
    )
    with pytest.raises(ValueError, match="defekte Spec"):
        specs.save_spec("broken", specs.SpecUpdate(project_fields={"description": "x"}))


def test_save_spec_json_stays_json(projects_dir, tmp_path):
    f = projects_dir / "pj.fabrik.json"
    f.write_text(
        json.dumps(
            {"name": "pj", "root": str(tmp_path), "loops": {"l1": {"steps": [{"type": "shell", "cmd": "echo x"}]}}}
        )
    )
    saved = specs.save_spec("pj", specs.SpecUpdate(project_fields={"description": "d"}))
    assert saved["description"] == "d"
    data = json.loads(f.read_text())  # Datei bleibt gültiges JSON
    assert data["description"] == "d"
    assert data["loops"]["l1"]["steps"][0]["cmd"] == "echo x"


def test_save_spec_merges_loop_preserving_unknown_keys(spec_file):
    """Pro-Loop-Merge: unbekannte Schlüssel (worktree, retry) der bestehenden
    Definition überleben, auch wenn der Payload sie nicht enthält."""
    update = specs.SpecUpdate(
        loops={"l1": {"description": "neu", "steps": [{"type": "shell", "name": "a2", "cmd": "echo a2"}]}}
    )
    specs.save_spec("p", update)
    text = spec_file.read_text()
    # l1: description + steps aus dem Payload, triggers aus der Original-Def erhalten.
    assert "description: neu" in text
    assert "echo a2" in text
    assert "type: manual" in text  # l1-trigger überlebt den Merge
    # l2 komplett unangetastet.
    assert "echo b" in text


def test_save_spec_remove_loops(spec_file):
    update = specs.SpecUpdate(remove_loops=["l2"])
    saved = specs.save_spec("p", update)
    assert "l2" not in saved["loops"]
    assert "l1" in saved["loops"]
    text = spec_file.read_text()
    assert "l2:" not in text
    assert "l1:" in text


def test_save_spec_optimistic_lock_conflict(spec_file):
    """base_hash ≠ aktueller Datei-Hash → SpecConflict, Datei bleibt unangetastet."""
    before = spec_file.read_text()
    stale = "0" * 64
    with pytest.raises(specs.SpecConflict):
        specs.save_spec("p", specs.SpecUpdate(project_fields={"description": "x"}), base_hash=stale)
    assert spec_file.read_text() == before


def test_save_spec_optimistic_lock_ok(spec_file):
    """base_hash = aktueller Hash → Save geht durch, neuer Hash wird geliefert."""
    update = specs.SpecUpdate(project_fields={"description": "neu"})
    saved = specs.save_spec("p", update, base_hash=specs.spec_hash(spec_file))
    assert saved["description"] == "neu"
    assert saved["_hash"] == specs.spec_hash(spec_file)  # Hash der neuen Datei


def test_save_spec_name_without_name_field(projects_dir, tmp_path):
    """Spec ohne `name:` → der gelieferte Name kommt aus der kanonischen Spec,
    nicht aus dem Temp-Pfad (früher: `p.yaml` statt `p`)."""
    (projects_dir / "p.fabrik.yaml").write_text(
        f'root: {tmp_path}\nloops:\n  l1:\n    steps:\n      - {{type: shell, cmd: "echo x"}}\n'
    )
    saved = specs.save_spec("p", specs.SpecUpdate(project_fields={"description": "d"}))
    assert saved["name"] == "p"


def test_save_spec_byte_preserves_untouched_sections(spec_file):
    """Unangetastete Sektionen bleiben byte-identisch (Sequenz-Indentation +
    keine Zeilen-Umbrüche) — nur die geänderten Felder ändern sich."""
    before = spec_file.read_text()
    specs.save_spec("p", specs.SpecUpdate(project_fields={"max_parallel": 4}))
    after = spec_file.read_text()
    # Die notify-Sektion (Sequenz-Indentation) bleibt exakt erhalten.
    assert "notify:\n  github_issue: true  # Inline-Kommentar (muss überleben)" in after
    # Nur max_parallel hat sich geändert.
    assert "max_parallel: 4" in after
    assert before.replace("max_parallel: 1", "max_parallel: 4") == after


# ---------------------------------------------------------------- load_spec_raw / spec_hash
def test_load_spec_raw_unexpanded(projects_dir, tmp_path):
    """load_spec_raw parst ohne Expansion: vars bleiben wörtlich, use: un-inlined,
    alle Loop-Schlüssel erhalten."""
    (projects_dir / "p.fabrik.yaml").write_text(
        f"root: {tmp_path}\nvars:\n  greet: hallo\nloops:\n"
        "  l1:\n    worktree: false\n    retry: {max: 2}\n"
        '    steps:\n      - {type: shell, cmd: "echo ${greet}"}\n'
        "  l2:\n    use: some-template\n"
    )
    raw = specs.load_spec_raw(projects_dir / "p.fabrik.yaml")
    assert raw["loops"]["l1"]["steps"][0]["cmd"] == "echo ${greet}"  # var wörtlich
    assert raw["loops"]["l1"]["worktree"] is False  # unbekannter Schlüssel erhalten
    assert raw["loops"]["l1"]["retry"] == {"max": 2}
    assert raw["loops"]["l2"]["use"] == "some-template"  # use: nicht inlined
    assert "vars" in raw  # Vars-Sektion bleibt (wird nicht expandiert/entfernt)


def test_spec_hash_changes_on_edit(spec_file):
    h1 = specs.spec_hash(spec_file)
    assert len(h1) == 64
    spec_file.write_text(spec_file.read_text() + "# change\n")
    assert specs.spec_hash(spec_file) != h1


# ---------------------------------------------------------------- SpecUpdate.from_payload
def test_spec_update_from_payload_ok():
    u = specs.SpecUpdate.from_payload({"loops": {"l": {}}, "description": "d", "max_parallel": 2})
    assert u.loops == {"l": {}}
    assert u.project_fields == {"description": "d", "max_parallel": 2}


def test_spec_update_from_payload_empty():
    u = specs.SpecUpdate.from_payload({})
    assert u.loops is None and u.project_fields == {} and u.remove_loops == []


def test_spec_update_from_payload_remove_loops():
    u = specs.SpecUpdate.from_payload({"loops": {"l": {}}, "remove_loops": ["a", "b"]})
    assert u.remove_loops == ["a", "b"]


def test_spec_update_from_payload_remove_loops_not_list():
    with pytest.raises(ValueError, match="remove_loops"):
        specs.SpecUpdate.from_payload({"remove_loops": "a"})


def test_spec_update_from_payload_unknown_field():
    with pytest.raises(ValueError, match="unbekannte Felder"):
        specs.SpecUpdate.from_payload({"name": "x"})


def test_spec_update_from_payload_loops_not_dict():
    with pytest.raises(ValueError, match="loops"):
        specs.SpecUpdate.from_payload({"loops": [1]})


def test_spec_update_from_payload_bad_types():
    with pytest.raises(ValueError, match="description"):
        specs.SpecUpdate.from_payload({"description": 5})
    with pytest.raises(ValueError, match="max_parallel"):
        specs.SpecUpdate.from_payload({"max_parallel": True})
    with pytest.raises(ValueError, match="max_parallel"):
        specs.SpecUpdate.from_payload({"max_parallel": "2"})


TEMPLATE_YAML = """\
params: {}
loop:
  triggers:
    - {type: cron}
  steps:
    - {type: shell, name: tpl-a, cmd: "echo a"}
    - {type: shell, name: tpl-b, cmd: "echo b"}
"""


@pytest.fixture
def use_spec_file(projects_dir, tmp_path):
    """Spec mit einer `use:`-Template-Loop ohne eigene triggers/steps."""
    (projects_dir / "templates").mkdir()
    (projects_dir / "templates" / "base.loop.yaml").write_text(TEMPLATE_YAML)
    f = projects_dir / "p.fabrik.yaml"
    f.write_text(f"name: p\nroot: {tmp_path}\nloops:\n  t:\n    use: base\n    description: alt\n")
    return f


def test_save_spec_use_loop_keeps_template_steps(use_spec_file):
    """Nur die description einer `use:`-Loop ändern: keine steps/triggers in
    der Datei, die expandierte Loop behält Template-Triggers + -Steps."""
    raw = specs.load_spec_raw(use_spec_file)["loops"]["t"]
    saved = specs.save_spec("p", specs.SpecUpdate(loops={"t": {**raw, "description": "neu"}}))
    loop = saved["loops"]["t"]
    assert [s["name"] for s in loop["steps"]] == ["tpl-a", "tpl-b"]
    assert loop["triggers"] == [{"type": "cron"}]
    text = use_spec_file.read_text()
    assert "description: neu" in text
    assert "steps:" not in text and "triggers:" not in text


def test_save_spec_use_loop_empty_steps_rejected(use_spec_file):
    """`steps: []` überschreibt die Template-Steps → Validierung schlägt fehl,
    die Datei bleibt unangetastet (Grund, warum der Client keine leeren Listen
    in `use:`-Loops schicken darf)."""
    before = use_spec_file.read_text()
    with pytest.raises(ValueError):
        specs.save_spec("p", specs.SpecUpdate(loops={"t": {"use": "base", "steps": []}}))
    assert use_spec_file.read_text() == before
