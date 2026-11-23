"""Browser-Tests (Playwright/Chromium) für die fabrik-UI.

Startet einen isolierten fabrik-HTTP-Server (eigene FABRIK_HOME mit Test-Spec +
Fake-Run) und prüft die interaktiven UI-Verhalten, die in den letzten Iterationen
repariert wurden:

- Projekt-Detail-View ist scrollbar (kein "blockiert" durch das Chat-Panel)
- Chat-Panel deckt Detail-Content nicht ab (Content scrollt darüber hinaus)
- Run-Panel ist ein Modal, das über dem Chat liegt (z-index)
- Run-Log ist scrollbar
- Loops sind klickbar → Spec klappt auf
- Akkordeon: nur eine Loop-Spec gleichzeitig offen
- Spec-Views (raw YAML + Loop-Spec) haben funktionierende Scrollbars

Laufzeit: ~10-20 s (Browser-Start + Interaktion). Mit `-m "not browser"`
ausblendbar, falls kein Chromium installiert ist.
"""

from __future__ import annotations

import json
import threading

import pytest

from fabrik import config

pytestmark = pytest.mark.browser

# Test-Projekt: 4 Loops (genug Content, damit die Detail-View bei kleiner
# Viewport-Höhe überläuft) + ein Loop mit vielen Steps (für Loop-Spec-Scroll).
SPEC_YAML = """\
name: uitest
description: "UI-Browser-Testprojekt"
root: __ROOT__
repo:
  branch: main
loops:
  lint:
    description: "Lint-Loop"
    triggers:
      - {type: manual}
    steps:
      - {type: shell, name: "check", cmd: "echo lint"}
  test:
    description: "Test-Loop"
    triggers:
      - {type: manual}
    steps:
      - {type: shell, name: "run-tests", cmd: "echo test", timeout: 60}
  develop:
    description: "Develop-Loop"
    triggers:
      - {type: manual}
    steps:
      - {type: git, name: "prepare", action: "prepare"}
      - {type: agent, name: "changes", backend: "opencode", prompt_file: "p.md", timeout: 3600}
      - {type: shell, name: "checks", cmd: "echo checks", timeout: 1800}
      - {type: pr, name: "pr", base: "main", title: "t", approval: true}
  big:
    description: "Loop mit vielen Steps (für Scroll-Test)"
    triggers:
      - {type: manual}
    steps:
      - {type: shell, name: "s01", cmd: "echo 1"}
      - {type: shell, name: "s02", cmd: "echo 2"}
      - {type: shell, name: "s03", cmd: "echo 3"}
      - {type: shell, name: "s04", cmd: "echo 4"}
      - {type: shell, name: "s05", cmd: "echo 5"}
      - {type: shell, name: "s06", cmd: "echo 6"}
      - {type: shell, name: "s07", cmd: "echo 7"}
      - {type: shell, name: "s08", cmd: "echo 8"}
      - {type: shell, name: "s09", cmd: "echo 9"}
      - {type: shell, name: "s10", cmd: "echo 10"}
      - {type: shell, name: "s11", cmd: "echo 11"}
      - {type: shell, name: "s12", cmd: "echo 12"}
      - {type: shell, name: "s13", cmd: "echo 13"}
      - {type: shell, name: "s14", cmd: "echo 14"}
      - {type: shell, name: "s15", cmd: "echo 15"}
      - {type: shell, name: "s16", cmd: "echo 16"}
      - {type: shell, name: "s17", cmd: "echo 17"}
      - {type: shell, name: "s18", cmd: "echo 18"}
      - {type: shell, name: "s19", cmd: "echo 19"}
      - {type: shell, name: "s20", cmd: "echo 20"}
"""

RUN_ID = "20260101T000000Z-uitest"
# Zweiter Run im Status needs_approval → testet die Approval-Buttons im Modal.
RUN_ID_APPROVAL = "20260102T000000Z-uitest-appr"
RUN_RECORD_APPROVAL = {
    "id": RUN_ID_APPROVAL,
    "project": "uitest",
    "loop": "develop",
    "status": "needs_approval",
    "trigger": "manual",
    "started": "2026-01-02T00:00:00Z",
    "waiting_step": "pr",
    "approval": {"what": "PR öffnen", "tier": 2},
    "steps": [
        {"name": "prepare", "type": "git", "status": "pass", "summary": "ok", "duration_s": 1.0},
    ],
}
# Älterer needs_approval-Run → testet die Sortierung (neueste zuerst) der
# Freigabe-Liste im Projekt-View.
RUN_ID_APPROVAL_OLD = "20260101T120000Z-uitest-appr-old"
RUN_RECORD_APPROVAL_OLD = {
    "id": RUN_ID_APPROVAL_OLD,
    "project": "uitest",
    "loop": "lint",
    "status": "needs_approval",
    "trigger": "manual",
    "started": "2026-01-01T12:00:00Z",
    "waiting_step": "check",
    "approval": {"what": "Shell-Befehl: echo alt", "tier": 1},
    "steps": [],
}
# 12 Steps mit je langem Output → Run-Log überläuft die Modal-Höhe (Scroll-Test).
RUN_RECORD = {
    "id": RUN_ID,
    "project": "uitest",
    "loop": "lint",
    "status": "pass",
    "trigger": "manual",
    "started": "2026-01-01T00:00:00Z",
    "finished": "2026-01-01T00:00:05Z",
    "steps": [
        {
            "name": f"step-{i:02d}",
            "type": "shell",
            "status": "pass",
            "summary": "ok",
            "duration_s": 1.2,
            "output": "\n".join(f"output line {j} of step {i}" for j in range(60)),
        }
        for i in range(12)
    ],
}
# Letzter Run einer erschöpften Retry-Kette („Abgelegt“-Liste, ADR 0036).
RUN_ID_SHELVED = "20260103T000000Z-uitest-slv"
RUN_RECORD_SHELVED = {
    "id": RUN_ID_SHELVED,
    "project": "uitest",
    "loop": "lint",
    "status": "fail",
    "trigger": "on_issue:#42",
    "trigger_key": "issue:o/r#42",
    "failure_kind": "transient",
    "error": "network timeout",
    "attempt": 3,
    "started": "2026-01-03T00:00:00Z",
    "finished": "2026-01-03T00:00:05Z",
}

# XSS-Regression (Issue #268): Namen mit Anführungszeichen und < > müssen
# gerendert, klickbar und code-frei sein. XSS_PROJECT deckt alle drei
# Sonderzeichen ab; XSS_BREAKOUT wäre in einem Inline-Handler
# (onclick="selectProject('…')") ein Code-Injection-Vektor.
XSS_PROJECT = "a'b\"c<d"
XSS_BREAKOUT = "x');window.__pwned=1;('"


@pytest.fixture
def fabrik_home(tmp_path, monkeypatch):
    """Isolierte FABRIK_HOME mit Test-Spec + Fake-Run (toucht echte Daten nicht)."""
    home = tmp_path / "fabrik_home"
    (home / "projects").mkdir(parents=True)
    (home / "runs" / "uitest").mkdir(parents=True)
    (home / "projects" / "uitest.fabrik.yaml").write_text(SPEC_YAML.replace("__ROOT__", str(home)))
    (home / "runs" / "uitest" / f"{RUN_ID}.json").write_text(json.dumps(RUN_RECORD))
    (home / "runs" / "uitest" / f"{RUN_ID_APPROVAL}.json").write_text(json.dumps(RUN_RECORD_APPROVAL))
    (home / "runs" / "uitest" / f"{RUN_ID_APPROVAL_OLD}.json").write_text(json.dumps(RUN_RECORD_APPROVAL_OLD))
    # Rohes Log des Fake-Runs → Test für den Log-Toggle im Run-Panel.
    (home / "runs" / "uitest" / f"{RUN_ID}.log").write_text(
        "[00:00:00] run start\n[00:00:01] step check: ok\n[00:00:05] run done\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "PROJECTS_DIR", home / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")  # pause/resume-Tests
    return home


@pytest.fixture
def fabrik_url(fabrik_home, live_server):
    """fabrik-HTTP-Server auf einem freien Port (ohne Scheduler)."""
    return live_server()


@pytest.fixture
def page(fabrik_url):
    """Chromium-Seite auf der UI (kleiner Viewport, damit Content überläuft;
    Browser-Sprache auf de-DE gepinnt, da die Assertions deutsche UI-Strings
    erwarten; i18n-Verhalten testet test_i18n_browser.py)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1280, "height": 560})
        pg.add_init_script("Object.defineProperty(navigator, 'language', {value: 'de-DE'});")
        pg.goto(fabrik_url)
        pg.wait_for_selector("#projects .project")
        yield pg
        browser.close()


def _select_project(page, name="uitest"):
    page.click(f"#projects .project:has-text('{name}')")
    page.wait_for_selector("#detail .loop")


def _open_run(page, run_id):
    """Run-Modal über die Runs-Liste (Historie) am Ende des Detail-Views öffnen."""
    page.click(f"#h-list .histrow[data-run='{run_id}'] .histmain")


def _open_chat(page):
    page.click("#chattoggle")
    page.wait_for_selector("#chatpanel:not(.hidden)")


def _zindex(page, sel):
    return int(page.eval_on_selector(sel, "el => getComputedStyle(el).zIndex"))


def test_project_view_shows_approval_count_badge(page):
    """Der Projekt-View zeigt die Anzahl offener Freigabe-Anfragen als Badge."""
    badge = page.locator("#projects .project .apprbadge")
    assert badge.count() == 1
    assert "2" in badge.inner_text()


def test_approval_badge_expands_list_without_selecting_project(page):
    """Badge-Klick klappt die Freigabe-Liste auf (neueste zuerst) — ohne das
    Projekt auszuwählen (stopPropagation)."""
    assert page.evaluate("state.selected") is None
    page.click("#projects .apprbadge")
    assert page.evaluate("state.selected") is None, "Badge-Klick darf das Projekt nicht auswählen"
    page.wait_for_selector("#projects .applist .apprline")
    lines = page.locator("#projects .applist .apprline")
    assert lines.count() == 2
    # Neueste zuerst: der 2026-01-02-Run vor dem 2026-01-01-Run.
    assert RUN_ID_APPROVAL in lines.nth(0).get_attribute("data-run")
    assert RUN_ID_APPROVAL_OLD in lines.nth(1).get_attribute("data-run")
    # Zweiter Klick klappt die Liste wieder ein.
    page.click("#projects .apprbadge")
    page.wait_for_function("document.querySelector('#projects .applist') === null")


def test_approval_list_item_opens_run_modal_with_reason(page):
    """Klick auf einen Listeneintrag öffnet das Run-Log-Modal mit dem
    Approval-Kontext (was/warum) wie üblich — inkl. Freigabe-Buttons."""
    page.click("#projects .apprbadge")
    page.wait_for_selector("#projects .applist .apprline")
    page.click("#projects .applist .apprline")
    page.wait_for_selector("#runpanel:not(.hidden)")
    page.wait_for_function("document.querySelector('#runstatus').dataset.status === 'needs_approval'")
    assert page.locator("#runactions button:has-text('freigeben')").count() == 1
    assert page.locator("#runactions button:has-text('ablehnen')").count() == 1
    # Approval-Kontext (was) ist gerendert.
    page.wait_for_selector("#runlog .runapproval")
    assert "PR öffnen" in page.eval_on_selector("#runlog .runapproval", "el => el.textContent")


def test_approval_list_survives_rerender(page):
    """Die aufgeklappte Freigabe-Liste überlebt Re-Renders (Auto-Refresh)."""
    page.click("#projects .apprbadge")
    page.wait_for_selector("#projects .applist .apprline")
    for _ in range(3):
        page.evaluate("renderProjects()")
        page.wait_for_timeout(50)
    assert page.locator("#projects .applist .apprline").count() == 2


def test_approval_list_tolerates_non_string_started(page):
    """Ein Record mit nicht-string `started` (vom Backend toleriert) bricht die
    Freigabe-Liste nicht ab."""
    html = page.evaluate(
        """() => {
            state.openApprovals = "p";
            return approvalList({name: "p", pending_approvals: [
                {id: "r1", loop: "develop", started: 42, approval: {what: "x"}},
            ]});
        }"""
    )
    assert 'class="apprline"' in html


def test_shelved_list_shows_exhausted_chain_with_actions(page, fabrik_home):
    """ADR 0036: Erschöpfte Kette als „Abgelegt“-Badge in der Projekt-Liste; Badge-Klick
    zeigt Klasse, letzten Fehler, Versuchszahl und die Aktionen *jetzt erneut* /
    *verwerfen* (analog Freigaben)."""
    (fabrik_home / "state").mkdir(exist_ok=True)
    (fabrik_home / "runs" / "uitest" / f"{RUN_ID_SHELVED}.json").write_text(json.dumps(RUN_RECORD_SHELVED))
    (fabrik_home / "state" / "retry-chains.json").write_text(
        json.dumps(
            {
                "uitest::lint::issue:o/r#42": {
                    "project": "uitest",
                    "loop": "lint",
                    "trigger_key": "issue:o/r#42",
                    "state": "exhausted",
                    "next_attempt_at": "",
                    "last_run_id": RUN_ID_SHELVED,
                    "claimed_run_id": "",
                    "created": "2026-01-01T00:00:00Z",
                    "updated": "2026-01-03T00:00:00Z",
                }
            }
        )
    )
    page.reload()
    page.wait_for_selector("#projects .shelvedbadge")
    badge = page.locator("#projects .shelvedbadge")
    assert badge.count() == 1
    assert "1" in badge.inner_text()
    page.click("#projects .shelvedbadge")  # Projekt wählen o. ä. muss nicht sein (stopPropagation)
    page.wait_for_selector("#projects .shelvedline")
    line = page.locator("#projects .shelvedline")
    assert line.count() == 1
    text = line.inner_text()
    assert "lint" in text
    assert "vorübergehend" in text
    assert "issue:o/r#42" in text
    assert "Versuch 3" in text
    assert "network timeout" in text
    assert line.locator("button.run", has_text="jetzt erneut").count() == 1
    assert line.locator("button.ghost", has_text="verwerfen").count() == 1


def test_detail_view_is_scrollable(page):
    """Die Projekt-Detail-View muss scrollen, wenn der Content überläuft."""
    _select_project(page)
    box = page.eval_on_selector(
        "#detail", "el => ({sh: el.scrollHeight, ch: el.clientHeight, oy: getComputedStyle(el).overflowY})"
    )
    assert box["oy"] == "auto"
    assert box["sh"] > box["ch"], f"Detail-View sollte überlaufen (sh={box['sh']}, ch={box['ch']})"
    # Scrollen funktioniert: scrollTop lässt sich setzen und bleibt.
    page.eval_on_selector("#detail", "el => { el.scrollTop = 120; }")
    assert page.eval_on_selector("#detail", "el => el.scrollTop") >= 100


def test_chat_does_not_block_detail_content(page):
    """Mit offenem Chat kann der Detail-Content darüber hinaus gescrollt werden."""
    _select_project(page)
    _open_chat(page)
    assert "chat-open" in page.get_attribute("body", "class")
    # Padding-bottom kompensiert die Chat-Höhe → Content scrollt über das Panel.
    pad = page.eval_on_selector("#detail", "el => parseInt(getComputedStyle(el).paddingBottom)")
    assert pad >= 340, f"padding-bottom sollte Chat-Höhe kompensieren (ist {pad})"
    # Letzten Loop nach oben scrollen → er liegt dann über dem Chat-Panel.
    page.eval_on_selector("#detail", "el => { el.scrollTop = el.scrollHeight; }")
    big = page.locator("#loop-big")
    assert big.is_visible()
    r = big.bounding_box()
    chat_top = page.eval_on_selector("#chatpanel", "el => el.getBoundingClientRect().top")
    assert r["y"] + r["height"] <= chat_top + 1, "Letzter Loop muss über dem Chat-Panel liegen"


def test_run_panel_is_modal_above_chat(page):
    """Run-Panel ist ein Modal mit höherem z-index als das Chat-Panel."""
    _select_project(page)
    _open_chat(page)
    _open_run(page, RUN_ID)
    page.wait_for_selector("#runpanel:not(.hidden)")
    assert _zindex(page, "#runpanel") > _zindex(page, "#chatpanel")
    # Backdrop ist sichtbar und liegt über dem Chat.
    assert page.locator("#runbackdrop").is_visible()
    assert _zindex(page, "#runbackdrop") > _zindex(page, "#chatpanel")
    # Element in der Mitte des Run-Panels gehört zum Run-Panel (nicht zum Chat).
    mid = page.eval_on_selector(
        "#runpanel",
        "el => { const r = el.getBoundingClientRect(); "
        "const t = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2); "
        "return t ? (t.closest('#runpanel') !== null) : false; }",
    )
    assert mid, "Mitte des Run-Panels muss vom Run-Panel belegt sein (nicht vom Chat)"


def test_run_log_is_scrollable(page):
    """Das Run-Log scrollt, wenn der Output lang ist."""
    _select_project(page)
    _open_run(page, RUN_ID)
    page.wait_for_selector("#runpanel:not(.hidden)")
    box = page.eval_on_selector(
        "#runlog", "el => ({sh: el.scrollHeight, ch: el.clientHeight, oy: getComputedStyle(el).overflowY})"
    )
    assert box["oy"] == "auto"
    assert box["sh"] > box["ch"], "Run-Log sollte überlaufen"
    page.eval_on_selector("#runlog", "el => { el.scrollTop = 200; }")
    assert page.eval_on_selector("#runlog", "el => el.scrollTop") >= 150


def test_run_modal_tabs_log_and_transcript(page):
    """Tabs Übersicht/Log/Transkript: Log kommt per SSE, Übersicht ist der Default."""
    _select_project(page)
    _open_run(page, RUN_ID)
    page.wait_for_selector("#runpanel:not(.hidden)")
    assert page.locator("#runlog .runrawlog").count() == 0
    page.click('#runtabs [data-tab="log"]')
    page.wait_for_function("document.querySelector('#runlog .runrawlog')?.textContent.includes('run done')")
    assert "run start" in page.eval_on_selector("#runlog .runrawlog", "el => el.textContent")
    assert page.is_visible("#runfollow")
    page.click('#runtabs [data-tab="transcript"]')
    page.wait_for_selector("#runlog .runtranscript")
    assert not page.is_visible("#runfollow")
    page.click('#runtabs [data-tab="overview"]')
    page.wait_for_function("document.querySelector('#runlog .runrawlog') === null")


def test_run_modal_trace_tab(page, fabrik_home):
    """#128: Trace-Tab zeigt die chronologische Step-Timeline aus dem Session-Log:
    Step-Header (Status, Dauer, Usage), Agent-Events als Drill-down, Artefakt-Liste."""
    from fabrik import sessionlog

    lg = sessionlog.SessionLog(RUN_ID, "uitest", "lint")
    lg.start()
    lg.event("step_start", step_name="step-00", step_type="shell")
    lg.event("prompt", step_name="step-00", step_type="agent", text="do the thing")
    lg.event("tool_call", step_name="step-00", step_type="agent", tool={"name": "bash", "cmd": "make"})
    lg.event("message", step_name="step-00", step_type="agent", text="all good")
    lg.event(
        "step_done",
        step_name="step-00",
        step_type="shell",
        status="pass",
        duration_s=5.0,
        usage={"model": "m", "total_tokens": 1234, "cost_usd": 0.02},
    )
    lg.artifact("step-00", "report", "/tmp/report.md")
    lg.finish("pass")

    # Verdict lives in the run record (not the session log): give step-00 one.
    rec_path = fabrik_home / "runs" / "uitest" / f"{RUN_ID}.json"
    rec = json.loads(rec_path.read_text())
    rec["steps"][0]["verdict"] = "pass"
    rec_path.write_text(json.dumps(rec))

    _select_project(page)
    _open_run(page, RUN_ID)
    page.wait_for_selector("#runpanel:not(.hidden)")
    page.click('#runtabs [data-tab="trace"]')
    page.wait_for_selector("#runlog .runtrace")
    page.wait_for_function("document.querySelector('#runlog .trace-step .trace-stepname')?.textContent === 'step-00'")
    assert page.locator("#runlog .trace-step").count() == 1
    head = page.eval_on_selector("#runlog .trace-step .trace-stephead", "el => el.textContent")
    assert "5s" in head and "1.2k" in head  # duration + compact token count
    assert page.locator("#runlog .trace-step .badge.verdict").count() == 1  # verdict from the run record
    # Agent-Events als (collapsed) Drill-down
    assert page.locator("#runlog .trace-event").count() == 3
    assert page.locator("#runlog .trace-event-tool_call").count() == 1
    # Artefakt-Liste
    assert page.locator("#runlog .trace-artifact").count() == 1
    assert "/tmp/report.md" in page.eval_on_selector("#runlog .trace-artifact", "el => el.textContent")


def test_run_log_live_update_and_follow_toggle(page, fabrik_home):
    """Neue Log-Zeilen eines laufenden Runs erscheinen ohne Reload; „folgen“ steuert Auto-Scroll."""
    rid = "20260103T000000Z-uitest-live"
    d = fabrik_home / "runs" / "uitest"
    log = d / f"{rid}.log"
    log.write_text("".join(f"early {i}\n" for i in range(200)))
    (d / f"{rid}.json").write_text(
        json.dumps({"id": rid, "project": "uitest", "loop": "lint", "status": "running", "steps": []})
    )
    _select_project(page)
    _open_run(page, rid)
    page.wait_for_selector("#runpanel:not(.hidden)")
    page.click('#runtabs [data-tab="log"]')
    page.wait_for_function("document.querySelector('#runlog .runrawlog')?.textContent.includes('early 199')")
    # folgen aktiv → ans Ende gescrollt
    assert page.eval_on_selector("#runlog", "el => el.scrollHeight - el.scrollTop - el.clientHeight") < 5
    with log.open("a") as f:
        f.write("LIVE-LINE\n")
    page.wait_for_function(
        "document.querySelector('#runlog .runrawlog').textContent.includes('LIVE-LINE')", timeout=2000
    )
    # folgen aus → Scrollposition bleibt bei neuen Zeilen
    page.uncheck("#runfollow")
    page.eval_on_selector("#runlog", "el => { el.scrollTop = 0; }")
    with log.open("a") as f:
        f.write("SECOND-LINE\n")
    page.wait_for_function(
        "document.querySelector('#runlog .runrawlog').textContent.includes('SECOND-LINE')", timeout=2000
    )
    assert page.eval_on_selector("#runlog", "el => el.scrollTop") == 0


def test_reject_updates_modal_and_removes_buttons(page):
    """Ablehnen aktualisiert das Modal: Status → rejected, Buttons verschwinden.

    Regression (Issue #50): Nach dem Klick auf "ablehnen" blieb das Modal
    unverändert (beide Buttons "freigeben"/"ablehnen" sichtbar), obwohl der
    Run bereits abgelehnt war.
    """
    _select_project(page)
    _open_run(page, RUN_ID_APPROVAL)
    page.wait_for_selector("#runpanel:not(.hidden)")
    # needs_approval → beide Buttons sind sichtbar.
    assert page.locator("#runactions button:has-text('freigeben')").count() == 1
    assert page.locator("#runactions button:has-text('ablehnen')").count() == 1
    # Ablehnen.
    page.click("#runactions button:has-text('ablehnen')")
    # Modal wird aktualisiert: Status rejected, keine Buttons mehr.
    page.wait_for_function("document.querySelector('#runstatus').dataset.status === 'rejected'")
    assert page.locator("#runactions button").count() == 0


def test_approve_follows_run_through_resume_race(page, monkeypatch):
    """Approve: Panel folgt dem Run durch das Resume-Race (needs_approval → running → pass).

    Regression: Nach "freigeben" schrieb der Resume-Worker "running" erst
    kurz NACH dem POST-Response → das UI-GET sah noch needs_approval, das
    Polling stoppte, und das Panel fror auf needs_approval ein, obwohl der
    Run längst lief. Der Server meldet jetzt `resumed: true`, und das UI
    pollt bedingungslos (mit needs_approval-Grace).
    """
    import fabrik.server as srv

    def fake_resume(proj, rid, approve, actor=None):
        # Simuliert das Race: der Worker schreibt "running" erst verzögert.
        def flip():
            import time

            p = config.RUNS_DIR / "uitest" / f"{RUN_ID_APPROVAL}.json"
            rec = json.loads(p.read_text())
            time.sleep(1.0)
            rec["status"] = "running"
            p.write_text(json.dumps(rec))
            time.sleep(1.0)
            rec["status"] = "pass"
            rec["finished"] = "2026-01-02T00:00:10Z"
            p.write_text(json.dumps(rec))

        threading.Thread(target=flip, daemon=True).start()
        return "▶ Approval — setzt fort", True

    monkeypatch.setattr(srv, "_chat_resume", fake_resume)
    _select_project(page)
    _open_run(page, RUN_ID_APPROVAL)
    page.wait_for_selector("#runpanel:not(.hidden)")
    page.click("#runactions button:has-text('freigeben')")
    # Panel muss den Run bis pass verfolgen (nicht auf needs_approval einfrieren).
    page.wait_for_function("document.querySelector('#runstatus').dataset.status === 'pass'", timeout=10000)
    assert page.locator("#runactions button").count() == 0


def test_approve_lock_busy_does_not_poll(page, monkeypatch):
    """Approve bei belegtem Projekt-Lock: Panel bleibt needs_approval, kein Polling.

    Der Server meldet `resumed: false` → das UI startet kein Polling (der Run
    wird erst fortgesetzt, wenn das andere Run fertig ist) und zeigt die
    Begründung aus der Antwort im Chat.
    """
    import fabrik.server as srv

    monkeypatch.setattr(srv, "_chat_resume", lambda proj, rid, approve, actor=None: ("⚠ uitest läuft bereits.", False))
    _select_project(page)
    _open_run(page, RUN_ID_APPROVAL)
    page.wait_for_selector("#runpanel:not(.hidden)")
    page.click("#runactions button:has-text('freigeben')")
    # Status bleibt needs_approval (Run nicht fortgesetzt) …
    page.wait_for_function("document.querySelector('#runstatus').dataset.status === 'needs_approval'")
    # … und das UI pollt nicht (kein offener Timer).
    assert page.evaluate("state.runTimer") is None


def _opacity(page, sel):
    """Sichtbare Opazität: Produkt über die Ahnen-Kette (opacity vererbt sich
    nicht als Computed Value, ein abgeblendeter Parent blendet aber mit ab)."""
    js = "e => { let o = 1; for (; e; e = e.parentElement) o *= +getComputedStyle(e).opacity; return o; }"
    return float(page.eval_on_selector(sel, js))


def test_disabled_loop_keeps_state_buttons_visible(fabrik_home, page):
    """Deaktivierter Loop: Inhalt abgeblendet, resume/pause bleiben voll sichtbar
    und klickbar; ▶ run ist gesperrt. resume aktiviert den Loop wieder."""
    from fabrik import pause

    pause.set_state("uitest", "lint", "disabled", actor="test")
    page.reload()
    page.wait_for_selector("#projects .project")
    _select_project(page)
    resume = "#loop-lint button.ghost:has-text('fortsetzen')"
    assert "disabled" in page.get_attribute("#loop-lint", "class")
    assert _opacity(page, resume) == 1
    assert _opacity(page, "#loop-lint .ldesc") < 1
    assert page.is_disabled("#loop-lint button.run")
    assert page.get_attribute("#loop-lint .runlock", "title") == "Loop deaktiviert — erst ▶ fortsetzen"
    page.click("#loop-lint")  # Spec aufklappen: auch sie wird abgeblendet
    page.wait_for_selector("#loopspec-lint:not(.hidden)")
    assert _opacity(page, "#loopspec-lint") < 1
    page.click(resume)
    page.wait_for_selector("#loop-lint:not(.disabled)")
    assert pause.effective_state("uitest", "lint") == "active"


def test_project_disabled_run_hint_points_to_project_resume(fabrik_home, page):
    """Projekt deaktiviert, Loop selbst aktiv: der Hinweis am gesperrten ▶ run
    verweist auf den Projekt-Kopf (die Loop-Karte hat dann keinen resume-Button)."""
    from fabrik import pause

    pause.set_state("uitest", None, "disabled", actor="test")
    page.reload()
    page.wait_for_selector("#projects .project")
    _select_project(page)
    assert "disabled" in page.get_attribute("#loop-lint", "class")
    assert page.locator("#loop-lint button.ghost:has-text('fortsetzen')").count() == 0
    assert "Projekt-Kopf" in page.get_attribute("#loop-lint .runlock", "title")


def test_loop_click_opens_spec(page):
    """Klick auf einen Loop klappt seine Spec auf."""
    _select_project(page)
    spec = page.locator("#loopspec-lint")
    assert spec.get_attribute("class").count("hidden") == 1
    page.click("#loop-lint")
    page.wait_for_selector("#loopspec-lint:not(.hidden)")
    assert "open" in page.get_attribute("#loop-lint", "class")
    # Warten, bis die Spec-Steps gerendert sind (async Fetch / Pre-Fetch).
    page.wait_for_selector("#loopspec-lint .loopspec-step", timeout=5000)
    assert page.locator("#loopspec-lint .loopspec-step").count() >= 1
    assert page.locator("#loopspec-lint").inner_text().count("check") >= 1


def test_loop_spec_is_accordion(page):
    """Nur eine Loop-Spec gleichzeitig offen (Akkordeon)."""
    _select_project(page)
    page.click("#loop-lint")
    page.wait_for_selector("#loopspec-lint:not(.hidden)")
    page.click("#loop-test")
    page.wait_for_selector("#loopspec-test:not(.hidden)")
    # lint ist wieder geschlossen, test offen.
    assert "hidden" in page.get_attribute("#loopspec-lint", "class")
    assert "open" not in page.get_attribute("#loop-lint", "class")
    assert "hidden" not in page.get_attribute("#loopspec-test", "class")
    assert "open" in page.get_attribute("#loop-test", "class")


def test_open_spec_survives_rerender(page):
    """Eine offene Loop-Spec überlebt (auch mehrere) Re-Renders (Auto-Refresh)."""
    _select_project(page)
    page.click("#loop-lint")
    page.wait_for_selector("#loopspec-lint .loopspec-step", timeout=5000)
    # Mehrere parallele/sequenzielle Re-Renders simulieren (5s-Interval +
    # pollRun-Refresh), wie sie im echten Betrieb auftreten.
    for _ in range(4):
        page.evaluate("renderDetail(state.selected)")
        page.wait_for_timeout(60)
    # Spec ist weiterhin offen (nicht automatisch kollabiert).
    assert "hidden" not in page.get_attribute("#loopspec-lint", "class")
    assert "open" in page.get_attribute("#loop-lint", "class")
    # Und ein anderer Loop lässt sich weiterhin öffnen (Akkordeon bleibt funktionsfähig).
    page.click("#loop-test")
    page.wait_for_selector("#loopspec-test:not(.hidden)")
    page.evaluate("renderDetail(state.selected)")
    page.wait_for_timeout(60)
    assert "hidden" not in page.get_attribute("#loopspec-test", "class")
    assert "hidden" in page.get_attribute("#loopspec-lint", "class")


def test_history_refreshes_while_loop_spec_open(page, fabrik_home):
    """Run-History aktualisiert sich auch bei offener Loop-Spec (ohne sie zu schließen).

    Regression: Der Auto-Refresh übersprang bei offener Spec den ganzen
    Detail-View — ein Run stand in der Run-Liste weiter auf "running", obwohl
    er längst needs_approval war (erst das Run-Modal zeigte den echten Status).
    """
    _select_project(page)
    page.click("#loop-lint")
    page.wait_for_selector("#loopspec-lint .loopspec-step", timeout=5000)
    rec_file = fabrik_home / "runs" / "uitest" / f"{RUN_ID}.json"
    rec_file.write_text(json.dumps({**RUN_RECORD, "status": "needs_approval"}))
    page.evaluate("refresh()")
    page.wait_for_selector(f"#h-list .histrow[data-run='{RUN_ID}'] .badge.needs_approval", timeout=5000)
    assert "hidden" not in page.get_attribute("#loopspec-lint", "class")


def test_specview_has_working_scrollbar(page):
    """Die raw-YAML-Spec-View scrollt (max-height + overflow)."""
    _select_project(page)
    page.click("#detail button:has-text('spec')")
    page.wait_for_selector("#specview:not(.hidden)")
    page.wait_for_timeout(200)  # YAML-Load
    box = page.eval_on_selector(
        "#specview", "el => ({sh: el.scrollHeight, ch: el.clientHeight, oy: getComputedStyle(el).overflowY})"
    )
    assert box["oy"] == "auto"
    assert box["sh"] > box["ch"], "Spec-View sollte überlaufen"
    page.eval_on_selector("#specview", "el => { el.scrollTop = 100; }")
    assert page.eval_on_selector("#specview", "el => el.scrollTop") >= 80


def test_loop_spec_has_working_scrollbar(page):
    """Eine Loop-Spec mit vielen Steps scrollt innerhalb ihrer Karte."""
    _select_project(page)
    page.click("#loop-big")
    page.wait_for_selector("#loopspec-big:not(.hidden)")
    page.wait_for_selector("#loopspec-big .loopspec-step", timeout=5000)  # Spec-Load
    box = page.eval_on_selector(
        "#loopspec-big", "el => ({sh: el.scrollHeight, ch: el.clientHeight, oy: getComputedStyle(el).overflowY})"
    )
    assert box["oy"] == "auto"
    assert box["sh"] > box["ch"], "Loop-Spec (20 Steps) sollte überlaufen"
    page.eval_on_selector("#loopspec-big", "el => { el.scrollTop = 100; }")
    assert page.eval_on_selector("#loopspec-big", "el => el.scrollTop") >= 80


def _open_history(page):
    _select_project(page)
    page.wait_for_selector("#h-list .histrow")


def _write_filler_runs(fabrik_home, n=60):
    """n zusätzliche lint-Runs (Dezember 2025); Run 3 trägt die Notiz 'needle-run'."""
    d = fabrik_home / "runs" / "uitest"
    for i in range(n):
        rid = f"20251201T{i:06d}Z-lint"
        rec = {**RUN_RECORD, "id": rid, "started": f"2025-12-01T00:{i // 60:02d}:{i % 60:02d}Z"}
        rec["note"] = "needle-run" if i == 3 else "filler"
        (d / f"{rid}.json").write_text(json.dumps(rec))


def _rows(page):
    return page.locator("#h-list .histrow").count()


def test_history_replaces_recent_runs_section(page):
    """Die Runs-Liste steht immer am Ende des Detail-Views (kein Toggle, kein "letzte Runs")."""
    _open_history(page)
    assert page.locator("#histtoggle").count() == 0
    assert page.locator("#hist").count() == 0
    is_last = page.eval_on_selector("#histview", "el => el.parentElement.lastElementChild === el")
    assert is_last


def test_history_search_filter_and_load_more(page, fabrik_home):
    """Historie: Suche, Status-Filter und "mehr laden" erreichen auch Runs jenseits der ersten Seite."""
    _write_filler_runs(fabrik_home)
    _open_history(page)
    assert _rows(page) == 20
    for n in (40, 60, 63):  # 60 + 3 Fixture-Runs
        page.click("#h-more")
        page.wait_for_function(f"document.querySelectorAll('#h-list .histrow').length === {n}")
    assert page.locator("#h-more.hidden").count() == 1
    # Volltext
    page.fill("#h-q", "needle-run")
    page.wait_for_function("document.querySelectorAll('#h-list .histrow').length === 1")
    assert "000003Z" in page.inner_text("#h-list")
    # Status + Datum
    page.fill("#h-q", "")
    page.select_option("#h-status", "needs_approval")
    page.wait_for_function("document.querySelectorAll('#h-list .histrow').length === 2")
    page.select_option("#h-status", "")
    page.fill("#h-to", "2025-12-31")
    page.wait_for_function("document.querySelectorAll('#h-list .histrow').length === 20")


def test_history_refresh_keeps_loaded_pages_and_scroll(page, fabrik_home):
    """Auto-Refresh lädt die geladenen Seiten in-place nach statt auf Seite 1 zurückzusetzen.

    Regression: Nach "mehr laden" setzte der 5-s-Refresh die Liste auf die erste
    Seite zurück — die Liste schrumpfte und die Scroll-Position sprang in die Specs.
    """
    _write_filler_runs(fabrik_home)
    _open_history(page)
    page.click("#h-more")
    page.wait_for_function("document.querySelectorAll('#h-list .histrow').length === 40")
    page.eval_on_selector("#detail", "el => { el.scrollTop = el.scrollHeight; }")
    top = page.eval_on_selector("#detail", "el => el.scrollTop")
    # Statusänderung eines bereits geladenen Runs → muss nach dem Refresh sichtbar sein.
    rid = "20251201T000050Z-lint"
    p = fabrik_home / "runs" / "uitest" / f"{rid}.json"
    p.write_text(json.dumps({**json.loads(p.read_text()), "status": "fail"}))
    page.evaluate("refresh()")
    page.wait_for_selector(f"#h-list .histrow[data-run='{rid}'] .badge.fail", timeout=5000)
    assert _rows(page) == 40
    assert page.eval_on_selector("#detail", "el => el.scrollTop") == top
    # "mehr laden" setzt nach dem Refresh korrekt fort (Cursor erhalten).
    page.click("#h-more")
    page.wait_for_function("document.querySelectorAll('#h-list .histrow').length === 60")


def test_history_filter_survives_rerender(page):
    """Ein Detail-Re-Render (Auto-Refresh) lässt Filter-Eingabe und Liste der Historie unberührt."""
    _open_history(page)
    page.fill("#h-q", "uitest-appr")
    page.wait_for_function("document.querySelectorAll('#h-list .histrow').length === 2")
    page.evaluate("refresh()")
    page.wait_for_timeout(300)
    assert page.input_value("#h-q") == "uitest-appr"
    assert _rows(page) == 2


def test_resume_updates_detail_while_loop_spec_open(page):
    """pause → resume aktualisiert Badge und Buttons sofort, auch bei offener Loop-Spec.

    Regression: Bei offener Loop-Spec übersprang refresh() den Detail-Render —
    nach "resume" blieb der Loop bis zum Browser-Reload als paused stehen.
    """
    _select_project(page)
    page.click("#loop-lint")
    page.wait_for_selector("#loopspec-lint .loopspec-step", timeout=5000)
    page.once("dialog", lambda dlg: dlg.accept(""))  # until-Prompt: leer = unbegrenzt
    page.click("#loop-lint button:has-text('pausieren')")
    page.wait_for_selector("#loop-lint .statebadge.paused")
    page.click("#loop-lint button:has-text('fortsetzen')")
    page.wait_for_function("document.querySelector('#loop-lint .statebadge') === null")
    assert page.locator("#loop-lint button:has-text('pausieren')").count() == 1
    assert "hidden" not in page.get_attribute("#loopspec-lint", "class")


def test_history_reject_from_list_with_confirmation(page):
    """needs_approval-Run lässt sich aus der Historie ablehnen — erst nach Bestätigung."""
    _open_history(page)
    row = page.locator(f".histrow[data-run='{RUN_ID_APPROVAL}']")
    assert page.locator(".histrow button").count() == 4  # nur die zwei needs_approval-Runs haben Aktionen
    page.once("dialog", lambda dlg: dlg.dismiss())
    row.locator("button:has-text('ablehnen')").click()
    page.wait_for_timeout(300)
    assert (
        json.loads((config.RUNS_DIR / "uitest" / f"{RUN_ID_APPROVAL}.json").read_text())["status"] == "needs_approval"
    )
    page.once("dialog", lambda dlg: dlg.accept())
    row.locator("button:has-text('ablehnen')").click()
    page.wait_for_function("document.querySelectorAll('.histrow button').length === 2")
    assert json.loads((config.RUNS_DIR / "uitest" / f"{RUN_ID_APPROVAL}.json").read_text())["status"] == "rejected"


def test_history_row_attributes_survive_quotes(page, fabrik_home):
    """Run-Ids mit Anführungszeichen bleiben im data-*-Attribut (esc maskiert Quotes)."""
    rid = 'x" data-pwned="1'
    rec = {**RUN_RECORD, "id": rid, "started": "2026-01-03T00:00:00Z"}
    (fabrik_home / "runs" / "uitest" / "quoted.json").write_text(json.dumps(rec))
    _open_history(page)
    assert page.locator("#h-list [data-pwned]").count() == 0
    assert page.locator("#h-list .histrow").first.get_attribute("data-run") == rid


def test_left_column_is_accordion_with_many_loops(page):
    """Nur das gewählte Projekt zeigt Loops; inaktive bleiben sichtbar, der
    Zustand überlebt Re-Renders, und es gibt keinen verschachtelten Scroll."""
    page.evaluate(
        """() => {
            const mk = (n, k) => ({name: n, description: "d", branch: "-", state: {state: "active"},
                pending_approvals: [],
                loops: Array.from({length: k}, (_, i) => ({name: `l${i}`, last: null}))});
            state.projects = [mk("a", 2), mk("b", 12), mk("c", 3)];
            state.selected = "b";
            renderProjects();
        }"""
    )
    assert page.locator("#projects .project").count() == 3
    assert page.locator("#projects .ploop").count() == 12
    assert "2 Loops" in page.locator("#projects .project").nth(0).inner_text()
    assert "3 Loops" in page.locator("#projects .project").nth(2).inner_text()
    # Inaktive Projekte per Scrollen der linken Spalte erreichbar und klickbar.
    page.locator("#projects .project").nth(2).scroll_into_view_if_needed()
    assert page.locator("#projects .project").nth(2).is_visible()
    for _ in range(3):
        page.evaluate("renderProjects()")
    assert page.locator("#projects .ploop").count() == 12
    # Kein verschachtelter Scrollbereich.
    assert page.eval_on_selector("#projects .ploops", "el => getComputedStyle(el).overflowY") == "visible"
    page.locator("#projects .project").nth(0).click()
    assert page.locator("#projects .ploop").count() == 2


def _write_failed_run(home, run_id, kind, started, steps=()):
    rec = {"id": run_id, "project": "uitest", "loop": "lint", "status": "fail", "trigger": "manual"}
    rec.update(started=started, finished=started, steps=list(steps))
    if kind:
        rec["failure_kind"] = kind
    (home / "runs" / "uitest" / f"{run_id}.json").write_text(json.dumps(rec))


def test_history_shows_and_filters_failure_kind(page, fabrik_home):
    """Fehlerklasse (ADR 0035): Badge in Historie und Run-Modal, Filter nach Klasse; Altbestand = unknown."""
    step = {"name": "scan", "type": "shell", "status": "fail", "summary": "exit 127", "failure_kind": "environment"}
    _write_failed_run(fabrik_home, "20260102T000000Z-uitest-env", "environment", "2026-01-02T00:00:00Z", [step])
    _write_failed_run(fabrik_home, "20260103T000000Z-uitest-flaky", "transient", "2026-01-03T00:00:00Z")
    _write_failed_run(fabrik_home, "20260104T000000Z-uitest-legacy", None, "2026-01-04T00:00:00Z")
    _open_history(page)
    page.wait_for_selector("#h-list .histrow[data-run='20260102T000000Z-uitest-env'] .badge.kind.environment")
    assert page.inner_text("#h-list .histrow[data-run='20260104T000000Z-uitest-legacy'] .badge.kind") == "unbekannt"
    assert page.locator(f"#h-list .histrow[data-run='{RUN_ID}'] .badge.kind").count() == 0  # pass: kein Badge
    page.select_option("#h-kind", "transient")
    page.wait_for_function("document.querySelectorAll('#h-list .histrow').length === 1")
    assert "uitest-flaky" in page.inner_text("#h-list")
    page.select_option("#h-kind", "")
    page.wait_for_function("document.querySelectorAll('#h-list .histrow').length > 1")
    _open_run(page, "20260102T000000Z-uitest-env")
    page.wait_for_selector("#runlog .runmeta .badge.kind.environment")
    assert page.locator("#runlog .runstep .badge.kind.environment").count() == 1


# ---- XSS-Regression (Issue #268): keine Inline-Handler, Namen in data-* ----

# Kein `name:`-Feld → Projektname = Dateiname-Stamm (spec_sources.default_name),
# damit die Sonderzeichen im Namen aus dem Dateinamen kommen.
_XSS_SPEC = """\
description: "XSS-Regression-Test"
root: __ROOT__
repo:
  branch: main
loops:
  lint:
    description: "Lint-Loop"
    triggers:
      - {type: manual}
    steps:
      - {type: shell, name: "check", cmd: "echo lint"}
"""


@pytest.fixture
def xss_home(tmp_path, monkeypatch):
    """Isolierte FABRIK_HOME mit zwei Projekten, deren Namen Anführungszeichen
    und < > enthalten (Projektname = Dateiname-Stamm, ADR 0047)."""
    home = tmp_path / "xss_home"
    (home / "projects").mkdir(parents=True)
    (home / "runs").mkdir(parents=True)
    spec = _XSS_SPEC.replace("__ROOT__", str(home))
    (home / "projects" / f"{XSS_PROJECT}.fabrik.yaml").write_text(spec, encoding="utf-8")
    (home / "projects" / f"{XSS_BREAKOUT}.fabrik.yaml").write_text(spec, encoding="utf-8")
    monkeypatch.setattr(config, "PROJECTS_DIR", home / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    return home


@pytest.fixture
def xss_page(xss_home, live_server):
    """Chromium-Seite auf der UI mit den XSS-Testprojekten."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1280, "height": 560})
        pg.goto(live_server())
        pg.wait_for_selector("#projects .project")
        yield pg
        browser.close()


def _find_project(page, name):
    """Projekt-Element per dataset.name finden (CSS-Selektor würde an den
    Anführungszeichen im Namen scheitern)."""
    return page.evaluate(
        """(n) => {
            const el = [...document.querySelectorAll('#projects .project')]
                .find((e) => e.dataset.name === n);
            return el ? { name: el.dataset.name, text: el.querySelector('.pname').textContent } : null;
        }""",
        name,
    )


def test_xss_quoted_names_render_clickable_no_exec(xss_page):
    """Namen mit ' " < werden gerendert, sind klickbar und führen keinen Code aus.

    Regressions-Schutz für Issue #268: wären die Werte in Inline-Handler
    (onclick="selectProject('…')") eingebettet, wäre XSS_BREAKOUT ein
    Injection-Vektor. Über data-* + Delegation bleiben sie harmlos."""
    page = xss_page
    # Beide Projekte sind in der Liste.
    assert page.locator("#projects .project").count() == 2
    # dataset.name liefert den Rohwert — die Werte sind Daten, kein Code.
    proj = _find_project(page, XSS_PROJECT)
    assert proj is not None
    assert proj["name"] == XSS_PROJECT
    # Der Name wird als Text gerendert (das < erzeugt kein Element).
    assert proj["text"].startswith(XSS_PROJECT)
    # Der Breakout-Name ist ebenfalls nur Daten (kein Code).
    assert _find_project(page, XSS_BREAKOUT)["name"] == XSS_BREAKOUT
    # Klick wählt das Projekt aus → Detail-View zeigt die Loop.
    page.evaluate(
        """(n) => {
            const el = [...document.querySelectorAll('#projects .project')]
                .find((e) => e.dataset.name === n);
            el.click();
        }""",
        XSS_PROJECT,
    )
    page.wait_for_selector("#detail .loop")
    assert page.evaluate("state.selected") == XSS_PROJECT
    # Kein Code wurde ausgeführt (wäre bei Inline-Handler + Breakout-Name der Fall).
    assert page.evaluate("window.__pwned") is None
