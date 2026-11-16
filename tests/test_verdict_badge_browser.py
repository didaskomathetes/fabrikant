"""Browser-Test: Verdikt-Badge (fachliches Ergebnis) neben dem Run-Status.

Ein echter Run, dessen Shell-Step grün endet und `::fabrik-verdict::fail` meldet (wie relay
audit-publish): Run-Status bleibt `pass`, die UI zeigt zusätzlich ein rotes Verdikt-Badge.
"""

from __future__ import annotations

import pytest

from fabrik import config, engine, runs

pytestmark = pytest.mark.browser

SPEC_YAML = """\
name: uitest
root: __ROOT__
loops:
  audit:
    triggers:
      - {type: manual}
    steps:
      - {type: shell, name: publish, cmd: "echo '::fabrik-verdict::fail'; echo 'relay #7 audit: fail'"}
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

    engine.run_loop("uitest", "audit", "manual")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.add_init_script("Object.defineProperty(navigator, 'language', {value: 'en-US'});")
        pg.goto(live_server())
        pg.wait_for_selector("#projects .project")
        yield pg
        browser.close()


def test_green_run_with_fail_verdict_shows_red_verdict_badge(page, fabrik_home):
    (rec,) = runs.list_runs("uitest")
    assert rec["status"] == "pass" and rec["steps"][0]["verdict"] == "fail"
    assert "fabrik-verdict" not in rec["steps"][0]["summary"]
    html = page.evaluate("(rec) => verdictBadge(rec)", rec)
    assert 'class="badge verdict bad"' in html and "verdict: fail" in html
    assert page.evaluate("(rec) => verdictBadge(rec)", {**rec, "steps": []}) == ""
    assert "verdict good" in page.evaluate("() => verdictBadge({verdict: 'pass'})")
    assert "verdict neutral" in page.evaluate("() => verdictBadge({verdict: 'active'})")
