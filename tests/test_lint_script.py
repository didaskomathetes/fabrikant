"""scripts/lint.sh: Gate (alle Checks, Exit 1 bei rot) und Sonar-Report-Modus (Funde ≠ Fehler)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lint.sh"
EX_FINDINGS = 1
EX_CONFIG = 78
# pyright --outputjson gibt JSON auf stdout aus; die übrigen Tools schreiben per --output-file / -o.
_PYRIGHT_OUT = '{"generalDiagnostics": [{"file": "/x/src/m.py", "severity": "error", "message": "m", "rule": "r", "range": {"start": {"line": 0}}}]}'


def _run(tmp_path: Path, uv: str, *args: str) -> subprocess.CompletedProcess:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "uv"
    stub.write_text(f"#!/bin/sh\n{uv}\n")
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)


def test_gate_green(tmp_path):
    p = _run(tmp_path, "exit 0")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "alle Checks grün" in p.stdout


def test_gate_runs_all_checks_and_lists_red_ones(tmp_path):
    p = _run(tmp_path, 'case "$*" in *pyright*|*bandit*) exit 1;; *) exit 0;; esac')
    assert p.returncode == EX_FINDINGS, p.stdout + p.stderr
    assert "ROT: pyright bandit" in p.stdout


def test_reports_mode_tolerates_findings_and_converts_pyright(tmp_path):
    uv = f"case \"$*\" in *pyright*) echo '{_PYRIGHT_OUT}'; exit 1;; *) exit 1;; esac"
    out = tmp_path / "reports"
    p = _run(tmp_path, uv, "--reports", str(out))
    assert p.returncode == 0, p.stdout + p.stderr
    sonar = json.loads((out / "pyright-sonar.json").read_text())
    assert sonar["issues"][0]["ruleId"] == "r"


@pytest.mark.parametrize("tool", ["ruff", "bandit", "pyright"])
def test_reports_mode_tool_error_is_config(tmp_path, tool):
    uv = f'case "$*" in *{tool}*) exit 2;; *pyright*) echo "{{}}";; esac'
    p = _run(tmp_path, uv, "--reports", str(tmp_path / "reports"))
    assert p.returncode == EX_CONFIG, p.stdout + p.stderr
    assert f"FEHLER: {tool}" in p.stdout


def test_reports_mode_broken_pyright_output_is_config(tmp_path):
    p = _run(tmp_path, 'case "$*" in *pyright*) echo "nicht json";; esac', "--reports", str(tmp_path / "r"))
    assert p.returncode == EX_CONFIG, p.stdout + p.stderr
    assert "FEHLER: pyright_sonar" in p.stdout
