"""Core: now_id, list_runs, gate logic (run_loop), spec loading, HTTP auth."""

import json
from pathlib import Path
from typing import Any

import pytest

import fabrik
from fabrik import specs


# ---------------------------------------------------------------- now_id
def test_now_id_unique():
    ids = {fabrik.now_id() for _ in range(2000)}
    assert len(ids) == 2000


def test_now_id_has_uuid_suffix():
    nid = fabrik.now_id()
    assert "-" in nid
    # volles uuid4 (128 Bit) — 6 Hex-Zellen hätten kollidieren können
    assert len(nid.split("-")[-1]) == 32


# ---------------------------------------------------------------- list_runs
def _seed_runs(runs_dir):
    (runs_dir / "A").mkdir()
    (runs_dir / "B").mkdir()
    (runs_dir / "A" / "old.json").write_text(json.dumps({"id": "old", "started": "2020-01-01T00:00:00Z"}))
    (runs_dir / "B" / "new.json").write_text(json.dumps({"id": "new", "started": "2024-01-01T00:00:00Z"}))


def test_list_runs_sorts_newest_first(runs_dir):
    _seed_runs(runs_dir)
    out = fabrik.list_runs(limit=50)
    assert [r["id"] for r in out] == ["new", "old"]


def test_list_runs_limit_truncates(runs_dir):
    _seed_runs(runs_dir)
    out = fabrik.list_runs(limit=1)
    assert len(out) == 1 and out[0]["id"] == "new"


def test_list_runs_project_filter(runs_dir):
    _seed_runs(runs_dir)
    out = fabrik.list_runs(project="A", limit=50)
    assert [r["id"] for r in out] == ["old"]


# ---------------------------------------------------------------- get_run_log
def test_get_run_log_reads_file(runs_dir):
    (runs_dir / "A").mkdir()
    (runs_dir / "A" / "r1.log").write_text("[10:00:00] start\n[10:00:01] done\n", encoding="utf-8")
    assert fabrik.get_run_log("r1") == "[10:00:00] start\n[10:00:01] done\n"
    # Projekt-Filter trifft dieselbe Datei.
    assert fabrik.get_run_log("r1", "A") == "[10:00:00] start\n[10:00:01] done\n"


def test_get_run_log_missing_is_none(runs_dir):
    (runs_dir / "A").mkdir()
    assert fabrik.get_run_log("keiner") is None
    # JSON ohne zugehörige .log → None (ältere Runs).
    (runs_dir / "A" / "only.json").write_text(json.dumps({"id": "only"}))
    assert fabrik.get_run_log("only") is None


def test_get_run_log_rejects_path_traversal(runs_dir):
    (runs_dir / "A").mkdir()
    (runs_dir / "A" / "ok.log").write_text("geheim\n", encoding="utf-8")
    for bad in ("../A/ok", "..", "A/ok", "a\\b", ""):
        assert fabrik.get_run_log(bad) is None
    # get_run wird gleich gesichert (gleiche Id landet im Dateipfad).
    assert fabrik.get_run("../A/ok") is None


# ---------------------------------------------------------------- gate logic
def _write_spec(projects_dir, name, steps):
    body = f"name: {name}\nroot: /tmp\nloops:\n  t:\n    steps:\n"
    for s in steps:
        body += f"      - type: shell\n        name: {s['name']}\n        cmd: {s['cmd']}\n"
        if "gate" in s:
            body += f"        gate: {'true' if s['gate'] else 'false'}\n"
    (projects_dir / f"{name}.fabrik.yaml").write_text(body)
    return name


def test_gate_true_stops_on_first_error(runs_dir, projects_dir):
    a = _write_spec(
        projects_dir,
        "tgate_a",
        [
            {"name": "s1", "cmd": "exit 1"},
            {"name": "s2", "cmd": "echo should-not-run"},
        ],
    )
    rec = fabrik.run_loop(a, "t")
    ran = {s["name"]: s["status"] for s in rec["steps"]}
    assert rec["status"] == "fail"
    assert "s2" not in ran


def test_gate_false_continues_past_failure(runs_dir, projects_dir):
    b = _write_spec(
        projects_dir,
        "tgate_b",
        [
            {"name": "s1", "cmd": "exit 1", "gate": False},
            {"name": "s2", "cmd": "echo ok"},
        ],
    )
    rec = fabrik.run_loop(b, "t")
    ran = {s["name"]: s["status"] for s in rec["steps"]}
    assert ran.get("s2") == "pass"
    assert rec["status"] == "fail"


def test_all_pass(runs_dir, projects_dir):
    c = _write_spec(
        projects_dir,
        "tgate_c",
        [
            {"name": "s1", "cmd": "echo ok"},
            {"name": "s2", "cmd": "echo ok"},
        ],
    )
    rec = fabrik.run_loop(c, "t")
    assert rec["status"] == "pass"


def test_ctx_exposes_fabrik_home(runs_dir, projects_dir):
    """{fabrik_home}: Prompts fremder Projekte finden fabriks Helfer-Scripts."""
    e = _write_spec(projects_dir, "tctx_home", [{"name": "s1", "cmd": "echo {fabrik_home}"}])
    rec = fabrik.run_loop(e, "t")
    assert rec["steps"][0]["summary"] == str(fabrik.config.FABRIK_HOME)


def test_run_loop_respects_given_run_id(runs_dir, projects_dir):
    """Vorgegebene run_id (Server: im POST-Response an die UI, bevor der
    Worker den Record schreibt) → Record + Log tragen exakt diese Id."""
    d = _write_spec(
        projects_dir,
        "tgate_d",
        [
            {"name": "s1", "cmd": "echo ok"},
        ],
    )
    rec = fabrik.run_loop(d, "t", run_id="20260101T000000Z-fixed-t")
    assert rec["id"] == "20260101T000000Z-fixed-t"
    assert (runs_dir / d / "20260101T000000Z-fixed-t.json").is_file()
    assert (runs_dir / d / "20260101T000000Z-fixed-t.log").is_file()


# ---------------------------------------------------------------- spec loading
def test_spec_root_absolute_loads(projects_dir, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "tabs.fabrik.yaml").write_text(
        f"name: tabs\nroot: {root}\nloops:\n  t:\n    steps:\n"
        "      - type: shell\n        name: s\n        cmd: echo hi\n"
    )
    spec = specs.load_spec(projects_dir / "tabs.fabrik.yaml")
    assert spec["root"] == str(root)


def test_spec_root_tilde_is_expanded(projects_dir):
    (projects_dir / "ttilde.fabrik.yaml").write_text(
        "name: ttilde\nroot: ~/definitely-not-here-xyz\nloops:\n  t:\n    steps:\n"
        "      - type: shell\n        name: s\n        cmd: echo hi\n"
    )
    with pytest.raises(ValueError, match=str(Path.home())):
        specs.load_spec(projects_dir / "ttilde.fabrik.yaml")


def test_spec_root_env_var_is_expanded(projects_dir, tmp_path, monkeypatch):
    """root: ${VAR} — dynamische Auflösung pro Deployment ohne YAML-Edit."""
    root = tmp_path / "envproj"
    root.mkdir()
    monkeypatch.setenv("FABRIK_TEST_ROOT", str(root))
    (projects_dir / "tenv.fabrik.yaml").write_text(
        "name: tenv\nroot: ${FABRIK_TEST_ROOT}\nloops:\n  t:\n    steps:\n"
        "      - type: shell\n        name: s\n        cmd: echo hi\n"
    )
    spec = specs.load_spec(projects_dir / "tenv.fabrik.yaml")
    assert spec["root"] == str(root)


def test_spec_root_env_var_with_tilde_value(projects_dir, monkeypatch):
    """`~` im VariablENWERT wird aufgelöst (expandvars → expanduser)."""
    monkeypatch.setenv("FABRIK_TEST_ROOT2", "~/definitely-not-here-env")
    (projects_dir / "tenv2.fabrik.yaml").write_text(
        "name: tenv2\nroot: ${FABRIK_TEST_ROOT2}\nloops:\n  t:\n    steps:\n"
        "      - type: shell\n        name: s\n        cmd: echo hi\n"
    )
    with pytest.raises(ValueError, match=str(Path.home())):
        specs.load_spec(projects_dir / "tenv2.fabrik.yaml")


def test_spec_verify_checks_non_list_rejected(projects_dir):
    (projects_dir / "tvnope.fabrik.yaml").write_text(
        "name: tvnope\nroot: /tmp\nloops:\n  t:\n    steps:\n"
        "      - type: verify\n        name: c\n        checks: shell\n"
    )
    with pytest.raises(ValueError, match="checks.*Liste"):
        specs.load_spec(projects_dir / "tvnope.fabrik.yaml")


def test_spec_verify_check_without_type_rejected(projects_dir):
    (projects_dir / "tvnotype.fabrik.yaml").write_text(
        "name: tvnotype\nroot: /tmp\nloops:\n  t:\n    steps:\n"
        "      - type: verify\n        name: c\n        checks:\n"
        "          - 42\n"
    )
    with pytest.raises(ValueError, match="verify-Check 0"):
        specs.load_spec(projects_dir / "tvnotype.fabrik.yaml")


def test_spec_verify_check_bogus_type_rejected(projects_dir):
    (projects_dir / "tvbogus.fabrik.yaml").write_text(
        "name: tvbogus\nroot: /tmp\nloops:\n  t:\n    steps:\n"
        "      - type: verify\n        name: c\n        checks:\n"
        "          - type: teleport\n"
    )
    with pytest.raises(ValueError, match="teleport"):
        specs.load_spec(projects_dir / "tvbogus.fabrik.yaml")


def test_spec_verify_valid_check_accepted(projects_dir, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (projects_dir / "tvgood.fabrik.yaml").write_text(
        f"name: tvgood\nroot: {root}\nloops:\n  t:\n    steps:\n"
        "      - type: verify\n        name: c\n        checks:\n"
        '          - type: shell\n            name: ok\n            cmd: "true"\n'
    )
    spec = specs.load_spec(projects_dir / "tvgood.fabrik.yaml")
    assert spec["loops"]["t"]["steps"][0]["type"] == "verify"


# ---------------------------------------------------------------- HTTP auth
def _handler(headers):
    h: Any = object.__new__(fabrik.FabrikHandler)  # Test-Doppel: Attribute werden ersetzt
    h.headers = headers
    h.path = "/api/runs"
    return h


def test_auth_no_origin_allowed(clean_token):
    assert _handler({})._origin_ok() is True


def test_auth_same_origin_allowed(clean_token):
    h = _handler({"Origin": "http://127.0.0.1:8321", "Host": "127.0.0.1:8321"})
    assert h._origin_ok() is True


def test_auth_cross_origin_blocked(clean_token):
    h = _handler({"Origin": "http://evil.example", "Host": "127.0.0.1:8321"})
    assert h._origin_ok() is False


def test_auth_no_token_set_allowed(clean_token):
    assert _handler({})._auth_ok() is True


def test_auth_correct_token_allowed(clean_token):
    clean_token.setenv("FABRIK_TOKEN", "secret123")
    assert _handler({"Authorization": "Bearer secret123"})._auth_ok() is True


def test_auth_wrong_token_blocked(clean_token):
    clean_token.setenv("FABRIK_TOKEN", "secret123")
    assert _handler({"Authorization": "Bearer nope"})._auth_ok() is False


# ---------------------------------------------------------------- static files
class _CapturingWfile:
    """Sammelt das per wfile.write() gesendete Body-Byte-Material."""

    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


def _static_handler(rel):
    """Handler-Doppel, der send_*-Aufrufe aufzeichnet (für _static-Tests)."""
    h: Any = object.__new__(fabrik.FabrikHandler)  # Test-Doppel: Attribute werden ersetzt
    h.headers = {}
    h.path = "/" + rel
    sent = {"code": None, "headers": []}
    h.send_response = lambda code: sent.__setitem__("code", code)
    h.send_header = lambda k, v: sent["headers"].append((k, v))
    h.end_headers = lambda: None
    h.wfile = _CapturingWfile()
    return h, sent


def test_static_sends_no_cache_header():
    """UI-Dateien werden mit Cache-Control: no-cache ausgeliefert (kein Stale-Cache)."""
    from fabrik import config

    assert (config.WEB_DIR / "app.js").is_file(), "Test setzt web/app.js voraus"
    h, sent = _static_handler("app.js")
    h._static("app.js")
    headers = dict(sent["headers"])
    assert sent["code"] == 200
    assert headers.get("Cache-Control") == "no-cache"
    assert headers.get("Content-Type") == "text/javascript"
