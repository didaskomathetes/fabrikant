"""deploy/*.service|timer — keine hartkodierten Checkout-Pfade; Services lösen den
Checkout über FABRIK_HOME aus ~/.config/fabrik/deploy.env auf (README, „Lokaler Betrieb")."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"
DEPLOY_ENV_FILE = "EnvironmentFile=-%h/.config/fabrik/deploy.env"
CD_FABRIK_HOME = 'cd "$${FABRIK_HOME:?'
# Absolute Home-/Maschinenpfade bzw. checkout-relative Annahmen unter %h.
_HARDCODED_PATH_RE = re.compile(r"/home/|/root/|PycharmProjects|hermes|%h/[\w.-]*quark-playground")

UNITS = sorted(p for p in DEPLOY_DIR.iterdir() if p.suffix in {".service", ".timer"})
SERVICES = [p for p in UNITS if p.suffix == ".service"]


def test_units_found():
    assert SERVICES, f"keine .service-Units in {DEPLOY_DIR}"


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_no_hardcoded_paths(unit: Path):
    hits = [
        f"{n}: {line}" for n, line in enumerate(unit.read_text().splitlines(), 1) if _HARDCODED_PATH_RE.search(line)
    ]
    assert not hits, f"{unit.name}: hartkodierter Pfad (FABRIK_HOME nutzen): {hits}"


@pytest.mark.parametrize("unit", SERVICES, ids=lambda p: p.name)
def test_service_resolves_checkout_via_fabrik_home(unit: Path):
    lines = unit.read_text().splitlines()
    assert DEPLOY_ENV_FILE in lines, f"{unit.name}: {DEPLOY_ENV_FILE} fehlt"
    assert not any(line.startswith("WorkingDirectory=") for line in lines), (
        f"{unit.name}: WorkingDirectory= expandiert keine Variablen — per `cd $FABRIK_HOME` im ExecStart lösen"
    )
    execs = [line for line in lines if line.startswith(("ExecStart=", "ExecStartPre="))]
    assert execs, f"{unit.name}: kein ExecStart"
    bad = [line for line in execs if CD_FABRIK_HOME not in line]
    assert not bad, f"{unit.name}: Exec ohne `cd $FABRIK_HOME`: {bad}"
