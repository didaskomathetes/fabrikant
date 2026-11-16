"""Browser-Test: Step-Leiste im Log-Tab (RunLogView) segmentiert das echte Engine-Log.

Der Test fährt einen echten 2-Step-Run, füttert dessen Log-Datei (Marker-Format der
Engine ist damit Teil des Vertrags) zeilenweise in die View und prüft aktiven Step,
Endzustände und den Klick-Filter.
"""

from __future__ import annotations

import pytest

from fabrik import config, engine

pytestmark = pytest.mark.browser

SPEC_YAML = """\
name: uitest
root: __ROOT__
loops:
  two:
    triggers:
      - {type: manual}
    steps:
      - {type: shell, name: "first step", cmd: "echo hello-one"}
      - {type: shell, name: "second", cmd: "echo hello-two && exit 1"}
"""


@pytest.fixture
def fabrik_home(tmp_path, monkeypatch):
    home = tmp_path / "fabrik_home"
    for d in ("projects", "runs", "state"):
        (home / d).mkdir(parents=True)
    (home / "projects" / "uitest.fabrik.yaml").write_text(SPEC_YAML.replace("__ROOT__", str(home)))
    monkeypatch.setattr(config, "PROJECTS_DIR", home / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    return home


@pytest.fixture
def page(fabrik_home, live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.add_init_script("Object.defineProperty(navigator, 'language', {value: 'en-US'});")
        pg.goto(live_server())
        pg.wait_for_selector("#projects .project")
        yield pg
        browser.close()


def _log_text(fabrik_home) -> str:
    engine.run_loop("uitest", "two", "manual")
    (log,) = (fabrik_home / "runs" / "uitest").glob("*.log")
    return log.read_text(encoding="utf-8")


def test_step_bar_tracks_running_step_and_filters(page, fabrik_home):
    lines = _log_text(fabrik_home).splitlines(keepends=True)
    start2 = next(i for i, ln in enumerate(lines) if "▶ second" in ln)
    page.evaluate("() => openRunPanel('uitest / two', 'running')")
    page.evaluate("() => { window.v = new RunLogView(document.querySelector('#runlog')); }")

    # Mitten im zweiten Step: erster fertig, zweiter aktiv.
    page.evaluate("(t) => v.append(t)", "".join(lines[: start2 + 1]))
    states = page.evaluate("() => v.segs.filter(s => !s.prelude).map(s => [s.name, s.state])")
    assert states == [["first step", "pass"], ["second", "running"]]
    assert page.locator(".logchip.running").inner_text().strip().endswith("second")

    # Rest inkl. Fehlschlag + Run-Ende.
    page.evaluate("(t) => v.append(t)", "".join(lines[start2 + 1 :]))
    states = page.evaluate("() => v.segs.filter(s => !s.prelude).map(s => [s.name, s.state])")
    assert states == [["first step", "pass"], ["second", "fail"]]
    assert page.locator(".logchip.running").count() == 0

    # Klick auf den ersten Step zeigt nur dessen Log.
    page.locator(".logchip", has_text="first step").click()
    assert page.locator(".logseg:visible", has_text="hello-one").count() == 1
    assert page.locator(".logseg:visible", has_text="hello-two").count() == 0
    page.locator(".logchip", has_text="all").click()
    assert page.locator(".logseg:visible", has_text="hello-two").count() == 1
