"""Browser-Tests (Playwright/Chromium) für die Graph-View (Cytoscape).

Die Graph-View ist ein DRAFT-Editor: sie rendert aus einer in-memory Working
Copy der Spec (``graphDrafts``), nicht aus dem Server-Payload. Hinzufügen/
Ändern/Entfernen/Reihenfolge von Loops & Steps ändert nur den Draft (Dry-Run)
und wird erst per "💾 save" in die Spec geschrieben. Details/Editor eines
Knotens erscheinen als schwebende Karte (``#graphnode``) IM Graph.

Startet einen isolierten fabrikant-HTTP-Server (eigene FABRIKANT_HOME mit Test-Spec)
und prüft die interaktiven Graph-Verhalten:

- Toggle ersetzt die Loop-Liste durch den Graph (Cytoscape rendert)
- has_loop-Kanten tragen das Trigger-Label (z. B. "manual"/"cron")
- Ghost-Nodes "+ New Loop" / "+ New Step" sind sichtbar
- Rechtsklick auf einen Loop-Node öffnet das Kontextmenü (run/pause/edit)
- Klick auf einen Loop-Node drillt auf dessen Steps herunter
- Drill-down: Steps als gerichtete Kette ab Loop-Wurzel (nur der erste Step
  hat eine Kante zur Loop; Ghost-Step am KETTENENDE)
- Klick auf den Projekt-Node (im Drill-down) geht eine Ebene zurück
- Klick auf einen Step-Node öffnet die schwebende Karte (Details + Edit)
- Per-Node-Edit: Step-Parameter / Loop-Name / Projekt-Description ändern →
  save schreibt die Spec, der Graph rendert die Änderung
- Dry-Run: Edits werden NICHT persistiert, bis "save" gedrückt wird
- discard setzt den Draft auf den letzten Save-Stand zurück (inkl. pending
  Loop-Rename)
- "✕ remove loop" entfernt eine Loop (Dry-Run bis save); gespeichert wird
  über die Save-Leiste (sichtbar bei ungespeicherten Änderungen, solange
  keine Karte offen ist)
- `use:`-Template-Loops (keine steps in der rohen Spec) blockieren den Save
  nicht, bekommen kein `steps: []`/`triggers: []` und zeigen die geerbten
  Template-Steps read-only (kein "+ New Step"); llm-`temperature` bleibt Float
- `use:`-Template-Loops: with:-Parameter sind in der Loop-Karte editierbar
  (Felder aus dem Template-params-Block) → save schreibt `with:` in die Spec
- Drill-down wählt Steps exakt per Kante (`lint` ≠ `lint:x`)
- State-Buttons im Kontextmenü schließen das Menü
- Generierte Loops (`relay:`-Block → relay-dispatch/-session/-audit) erscheinen
  read-only (`gloop`, Steps als `tstep`), Kontextmenü ohne edit, Save schreibt
  sie nicht in die YAML; generierte Loops und Template-Steps haben read-only
  Detailkarten (Rechtsklick „ⓘ details" bzw. Klick auf den Step)

Die Spec nutzt nur manual/cron-Trigger (on_pr/on_issue bräuchten ein Repo).
Laufzeit: ~10-20 s. Mit `-m "not browser"` ausblendbar.
"""

from __future__ import annotations

import pytest

from fabrikant import config, specs

pytestmark = pytest.mark.browser

# Zwei Loops mit je einem Trigger (manual + cron) → zwei has_loop-Kanten mit
# unterschiedlichen Labels/Farben. lint hat zwei Steps (Drill-down-Test).
SPEC_YAML = """\
name: uitest
root: __ROOT__
loops:
  lint:
    triggers:
      - {type: manual}
    steps:
      - {type: shell, name: "check", cmd: "echo lint"}
      - {type: shell, name: "fix", cmd: "echo fix"}
  cronloop:
    triggers:
      - {type: cron}
    steps:
      - {type: shell, name: "tick", cmd: "echo tick"}
"""


@pytest.fixture
def fabrikant_home(tmp_path, monkeypatch):
    """Isolierte FABRIKANT_HOME mit Test-Spec (toucht echte Daten nicht)."""
    home = tmp_path / "fabrikant_home"
    (home / "projects").mkdir(parents=True)
    (home / "runs").mkdir(parents=True)
    (home / "state").mkdir(parents=True)
    (home / "projects" / "uitest.fabrikant.yaml").write_text(SPEC_YAML.replace("__ROOT__", str(home)))
    monkeypatch.setattr(config, "PROJECTS_DIR", home / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    return home


@pytest.fixture
def page(fabrikant_home, live_server):
    """Chromium-Seite auf der UI (Browser-Sprache auf de-DE gepinnt, da die
    Assertions deutsche UI-Strings erwarten; i18n-Verhalten testet
    test_i18n_browser.py)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1280, "height": 800})
        pg.add_init_script("Object.defineProperty(navigator, 'language', {value: 'de-DE'});")
        pg.goto(live_server())
        pg.wait_for_selector("#projects .project")
        yield pg
        browser.close()


def _node_xy(page, node_id):
    """Rendered Screen-Koordinaten eines Cytoscape-Nodes (für Maus-Klicks)."""
    return page.evaluate(
        """(id) => {
            const el = document.querySelector('#graphcanvas');
            if (!el || !el._cy) return null;
            const n = el._cy.nodes('[id="' + id + '"]');
            if (n.length === 0) return null;
            const pos = n.renderedPosition();
            const r = el.getBoundingClientRect();
            return { x: r.left + pos.x, y: r.top + pos.y };
        }""",
        node_id,
    )


def _cy(page):
    """Kurzform: Cytoscape-Instanz des Graph-Canvas (null-safe)."""
    return "document.querySelector('#graphcanvas')._cy"


def _open_graph(page):
    """Projekt wählen und die Graph-View öffnen (Toggle)."""
    page.click("#projects .project")
    page.wait_for_selector("#detail .loop")
    page.click("button:has-text('graph')")
    page.wait_for_function("document.querySelector('#graphcanvas')._cy !== null", timeout=5000)
    # breadthfirst-Layout abwarten, bis die Nodes an ihrer Endposition sind.
    page.wait_for_timeout(800)


def _click_node(page, node_id):
    """Linksklick auf einen Node (per Rendered-Position)."""
    xy = _node_xy(page, node_id)
    assert xy is not None, f"Node {node_id} muss gerendert sein"
    page.mouse.click(xy["x"], xy["y"])


def _rightclick_node(page, node_id):
    """Rechtsklick auf einen Node (per Rendered-Position)."""
    xy = _node_xy(page, node_id)
    assert xy is not None, f"Node {node_id} muss gerendert sein"
    page.mouse.click(xy["x"], xy["y"], button="right")


def _open_card(page, node_id):
    """Rechtsklick auf einen Node → "✎ edit" → schwebende Karte warten."""
    _rightclick_node(page, node_id)
    page.wait_for_function("!document.querySelector('#graphctx').classList.contains('hidden')", timeout=3000)
    page.click("#graphctx button:has-text('bearbeiten')")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)


def _wait_graph_settled(page, node_id):
    """Wartet, bis der Graph den Node zeigt und das breadthfirst-Layout
    abgeklungen ist (~500 ms Animation). Ohne die Wartezeit liegt ein
    Maus-Klick auf einer veralteten (Animations-)Position."""
    page.wait_for_function(
        f"() => {{ const el = document.querySelector('#graphcanvas');"
        f" return el && el._cy && el._cy.nodes('[id=\"{node_id}\"]').length === 1; }}",
        timeout=5000,
    )
    page.wait_for_timeout(800)


def _drill_into_lint(page):
    """Drill-down auf die Steps der lint-Loop (Klick auf den Loop-Node)."""
    _click_node(page, "loop:uitest:lint")
    page.wait_for_function(
        f"() => {_cy(page)}.nodes().some(n => n.data('kind') === 'step')",
        timeout=5000,
    )


def test_graph_toggle_renders_cytoscape(page):
    """Der Toggle ersetzt die Loop-Liste durch den Graph (Cytoscape rendert)."""
    _open_graph(page)
    assert page.evaluate("state.graphOpen") == "uitest"
    # Top-Ebene: Projekt + beide Loops + Ghost-Node "+ New Loop".
    kinds = page.evaluate(f"() => {_cy(page)}.nodes().map(n => n.data('kind'))")
    assert sorted(kinds) == ["ghost", "loop", "loop", "project"]


def test_graph_has_loop_edges_carry_trigger_label(page):
    """has_loop-Kanten tragen das Trigger-Label der Loop (im computed Style)."""
    _open_graph(page)
    labels = page.evaluate(
        f"() => {_cy(page)}.edges().filter(e => e.data('kind') === 'has_loop').map(e => e.style('label'))"
    )
    assert sorted(labels) == ["cron", "manuell"]


def test_graph_ghost_nodes_visible(page):
    """Ghost-Nodes zum Hinzufügen sind sichtbar: "+ New Loop" (Top) und
    "+ New Step" (Drill-down)."""
    _open_graph(page)
    assert page.evaluate(f"() => {_cy(page)}.nodes('[id=\"ghost:loop\"]').length") == 1
    _drill_into_lint(page)
    assert page.evaluate(f"() => {_cy(page)}.nodes('[id=\"ghost:step\"]').length") == 1


def test_graph_rightclick_loop_opens_context_menu(page):
    """Rechtsklick auf einen Loop-Node öffnet das Kontextmenü mit den
    Loop-Karten-Aktionen (run/pause/edit)."""
    _open_graph(page)
    _rightclick_node(page, "loop:uitest:lint")
    page.wait_for_function("!document.querySelector('#graphctx').classList.contains('hidden')", timeout=3000)
    menu = page.locator("#graphctx")
    assert menu.locator("button:has-text('starten')").count() == 1
    assert menu.locator("button:has-text('pausieren')").count() == 1
    assert menu.locator("button:has-text('bearbeiten')").count() == 1


def test_graph_drilldown_and_back(page):
    """Klick auf einen Loop-Node drillt auf dessen Steps herunter; Klick auf den
    Projekt-Node (der im Drill-down sichtbar bleibt) geht eine Ebene zurück."""
    _open_graph(page)
    _click_node(page, "loop:uitest:lint")
    page.wait_for_function(
        f"() => {_cy(page)}.nodes().some(n => n.data('kind') === 'step')",
        timeout=5000,
    )
    # Drill-down: Projekt + Loop + die zwei Steps von lint + Ghost-Step.
    ids = page.evaluate(f"() => {_cy(page)}.nodes().map(n => n.data('id'))")
    assert "proj:uitest" in ids
    assert "loop:uitest:lint" in ids
    assert "step:uitest:lint:0" in ids
    assert "step:uitest:lint:1" in ids
    assert "ghost:step" in ids
    # Zurück: Projekt-Node anklicken.
    _click_node(page, "proj:uitest")
    page.wait_for_function(
        f"() => !{_cy(page)}.nodes().some(n => n.data('kind') === 'step')",
        timeout=5000,
    )
    kinds = page.evaluate(f"() => {_cy(page)}.nodes().map(n => n.data('kind'))")
    assert sorted(kinds) == ["ghost", "loop", "loop", "project"]


def test_graph_drilldown_steps_form_directed_chain(page):
    """Drill-down: Steps bilden eine gerichtete Kette ab Loop-Wurzel — nur der
    ERSTE Step hat eine Kante zur Loop (has_step), die weiteren folgen per
    next_step; der Ghost-Step hängt am KETTENENDE, nicht an der Loop."""
    _open_graph(page)
    _drill_into_lint(page)
    edges = page.evaluate(f"() => {_cy(page)}.edges().map(e => [e.data('kind'), e.data('source'), e.data('target')])")
    # Genau eine has_step-Kante: Loop → erster Step (kein Stern auf alle Steps).
    assert [e for e in edges if e[0] == "has_step"] == [
        ["has_step", "loop:uitest:lint", "step:uitest:lint:0"],
    ]
    # Kette: s0 → s1 per next_step (keine direkte Loop → s1-Kante).
    assert [e for e in edges if e[0] == "next_step"] == [
        ["next_step", "step:uitest:lint:0", "step:uitest:lint:1"],
    ]
    # Ghost-Step hängt am KETTENENDE (letzter Step), nicht an der Loop.
    assert [e for e in edges if e[0] == "ghost_edge"] == [
        ["ghost_edge", "step:uitest:lint:1", "ghost:step"],
    ]


def test_graph_step_click_opens_card(page):
    """Klick auf einen Step-Node öffnet die schwebende Karte (#graphnode) mit
    den Details/Parametern des Steps (kein Bottom-Panel, kein In-Place-Label)."""
    _open_graph(page)
    _drill_into_lint(page)
    _click_node(page, "step:uitest:lint:0")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    # Die Karte zeigt den Step (Name + Typ + cmd-Feld).
    assert page.locator("#graphnode input[data-ge='name']").input_value() == "check"
    cmd = page.locator("#graphnode textarea[data-ge='cmd']")
    assert cmd.count() == 1
    assert cmd.input_value() == "echo lint"


def test_graph_step_rightclick_opens_context_menu(page):
    """Rechtsklick auf einen Step-Node öffnet das Kontextmenü mit edit
    (schwebende Karte für den Step)."""
    _open_graph(page)
    _drill_into_lint(page)
    _rightclick_node(page, "step:uitest:lint:0")
    page.wait_for_function("!document.querySelector('#graphctx').classList.contains('hidden')", timeout=3000)
    menu = page.locator("#graphctx")
    assert menu.locator("button:has-text('bearbeiten')").count() == 1


def test_graph_edit_step_param_change_saves(page, fabrikant_home):
    """Per-Node-Edit: Step-Karte öffnen, Parameter (cmd) ändern → save schreibt
    die Spec; der übrige Step bleibt erhalten."""
    _open_graph(page)
    _drill_into_lint(page)
    _click_node(page, "step:uitest:lint:0")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    page.locator("#graphnode textarea[data-ge='cmd']").fill("echo lint v2")
    page.click("#graphnode button:has-text('speichern')")
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=5000)
    text = (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()
    assert "echo lint v2" in text
    assert "echo fix" in text  # übriger Step bleibt erhalten


def test_graph_edit_loop_rename_saves_and_rerenders(page, fabrikant_home):
    """Loop-Karte: Loop umbenennen → save: Datei wird geschrieben, Graph rendert
    die Änderung; die übrige Loop bleibt erhalten."""
    _open_graph(page)
    _open_card(page, "loop:uitest:lint")
    page.fill("#graphnode input[data-gn='name']", "lint2")
    page.click("#graphnode button:has-text('speichern')")
    # Save abgeschlossen: Karte schließt (PUT + Re-Render fertig).
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=5000)
    _wait_graph_settled(page, "loop:uitest:lint2")
    ids = page.evaluate(f"() => {_cy(page)}.nodes().map(n => n.data('id'))")
    assert "loop:uitest:lint" not in ids
    assert "loop:uitest:cronloop" in ids  # übrige Loops bleiben erhalten
    text = (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()
    assert "lint2:" in text
    assert "cronloop:" in text


def test_graph_edit_project_description(page, fabrikant_home):
    """Projekt-Karte: description wird in die Spec-Datei geschrieben."""
    _open_graph(page)
    _click_node(page, "proj:uitest")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    page.fill("#graphnode input[data-gn='description']", "neue Beschreibung")
    page.click("#graphnode button:has-text('speichern')")
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=5000)
    text = (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()
    assert "description: neue Beschreibung" in text


def test_graph_ghost_loop_adds_node_dryrun(page, fabrikant_home):
    """Ghost-Node "+ New Loop" fügt eine Loop in den Draft (Dry-Run) hinzu —
    die Datei bleibt unverändert, bis "save" gedrückt wird."""
    _open_graph(page)
    _click_node(page, "ghost:loop")
    # Draft hat die neue Loop; die Karte ist offen.
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    assert page.evaluate("() => Object.keys(graphDrafts['uitest'].loops).includes('new-loop')") is True
    # Dry-Run: Datei noch NICHT geändert.
    text = (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()
    assert "new-loop" not in text
    # Save → Datei wird geschrieben.
    page.click("#graphnode button:has-text('speichern')")
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=5000)
    text = (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()
    assert "new-loop:" in text


def test_graph_ghost_step_adds_node_dryrun(page, fabrikant_home):
    """Ghost-Node "+ New Step" (Drill-down) hängt einen Step an — Dry-Run bis save."""
    _open_graph(page)
    _drill_into_lint(page)
    _click_node(page, "ghost:step")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    # Draft: lint hat jetzt drei Steps.
    assert page.evaluate("() => graphDrafts['uitest'].loops.lint.steps.length") == 3
    # Dry-Run: Datei noch NICHT geändert (lint hat in der Datei zwei Steps).
    text = (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()
    assert text.count("type: shell") == 3  # check, fix, tick — kein new-step
    # Save → Datei wird geschrieben (lint hat jetzt drei Steps).
    page.click("#graphnode button:has-text('speichern')")
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=5000)
    text = (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()
    assert "new-step" in text


def test_graph_dryrun_edit_not_persisted_until_save(page, fabrikant_home):
    """Dry-Run: ein Feld-Change in der Karte persistiert NICHT, bis "save"."""
    _open_graph(page)
    _drill_into_lint(page)
    _click_node(page, "step:uitest:lint:1")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    page.locator("#graphnode textarea[data-ge='cmd']").fill("echo fix CHANGED")
    # Noch nicht gespeichert: Datei zeigt den alten Wert.
    text = (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()
    assert "echo fix CHANGED" not in text
    assert "echo fix" in text
    # Save → Datei wird aktualisiert.
    page.click("#graphnode button:has-text('speichern')")
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=5000)
    text = (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()
    assert "echo fix CHANGED" in text


def test_graph_discard_reverts_changes(page):
    """discard setzt den Draft auf den letzten Save-Stand zurück (Karte zeigt
    wieder den Originalwert)."""
    _open_graph(page)
    _drill_into_lint(page)
    _click_node(page, "step:uitest:lint:0")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    page.locator("#graphnode textarea[data-ge='cmd']").fill("echo lint DISCARDED")
    page.click("#graphnode button:has-text('verwerfen')")
    # Karte rendert den Draft neu → Originalwert.
    page.wait_for_function(
        "() => document.querySelector('#graphnode textarea[data-ge=\"cmd\"]').value === 'echo lint'",
        timeout=3000,
    )
    assert page.locator("#graphnode textarea[data-ge='cmd']").input_value() == "echo lint"


def test_graph_discard_resets_pending_rename(page):
    """discard verwirft auch ein pending Loop-Rename: das Name-Feld zeigt nach
    dem Discard wieder den Originalnamen (und der nächste Save renamt nicht)."""
    _open_graph(page)
    _open_card(page, "loop:uitest:lint")
    page.fill("#graphnode input[data-gn='name']", "renamed")
    page.click("#graphnode button:has-text('verwerfen')")
    page.wait_for_function(
        "() => document.querySelector('#graphnode input[data-gn=\"name\"]').value === 'lint'",
        timeout=3000,
    )
    assert page.locator("#graphnode input[data-gn='name']").input_value() == "lint"
    assert page.evaluate("() => graphSel.name") is None  # Rename verworfen


def test_graph_remove_loop_dryrun_and_save(page, fabrikant_home):
    """'✕ remove loop' entfernt die Loop aus dem Draft (Dry-Run); erst save
    schreibt die Entfernung in die Spec-Datei."""
    _open_graph(page)
    _open_card(page, "loop:uitest:cronloop")
    page.click("#graphnode button:has-text('Loop entfernen')")
    # Karte schließt; die Loop ist aus dem Draft weg.
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    assert page.evaluate("() => Object.keys(graphDrafts['uitest'].loops)") == ["lint"]
    # Dry-Run: Datei noch unverändert.
    text = (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()
    assert "cronloop:" in text
    # Karte ist zu → Save über die Save-Leiste (ohne die gäbe es keinen
    # sichtbaren Save-Button mehr) → Loop wird aus der Datei entfernt.
    page.wait_for_selector("#graphbar:not(.hidden)", timeout=3000)
    page.click("#graphbar button:has-text('speichern')")
    page.wait_for_selector("#graphbar.hidden", state="attached", timeout=5000)
    text = (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()
    assert "cronloop:" not in text
    assert "lint:" in text  # übrige Loop bleibt erhalten


def test_graph_save_bar_follows_dirty_state(page, fabrikant_home):
    """Save-Leiste: versteckt ohne Änderungen und bei offener Karte, sichtbar
    nach einem Edit bei geschlossener Karte; discard darüber stellt den Draft
    wieder her."""
    _open_graph(page)
    assert page.locator("#graphbar").get_attribute("class") == "graphbar hidden"
    _open_card(page, "loop:uitest:cronloop")
    page.click("#graphnode button:has-text('Loop entfernen')")
    page.wait_for_selector("#graphbar:not(.hidden)", timeout=3000)
    # Offene Karte (eigenes save/discard) blendet die Leiste aus — keine
    # Überlappung; Schließen der Karte zeigt sie wieder.
    _open_card(page, "loop:uitest:lint")
    page.wait_for_selector("#graphbar.hidden", state="attached", timeout=3000)
    page.click("#graphnode button[title='schließen']")
    page.wait_for_selector("#graphbar:not(.hidden)", timeout=3000)
    page.click("#graphbar button:has-text('verwerfen')")
    page.wait_for_selector("#graphbar.hidden", state="attached", timeout=3000)
    assert sorted(page.evaluate("() => Object.keys(graphDrafts['uitest'].loops)")) == ["cronloop", "lint"]
    assert "cronloop:" in (fabrikant_home / "projects" / "uitest.fabrikant.yaml").read_text()


def _add_template_loop(fabrikant_home):
    """Template `base` (cron-Trigger + 2 Steps) + Loop `templ` mit `use: base`
    ohne eigene triggers/steps an die Test-Spec hängen."""
    spec_file = fabrikant_home / "projects" / "uitest.fabrikant.yaml"
    tpl_dir = fabrikant_home / "projects" / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "base.loop.yaml").write_text(
        "params: {}\nloop:\n  triggers:\n    - {type: cron}\n  steps:\n"
        '    - {type: shell, name: tpl-a, cmd: "echo a"}\n'
        '    - {type: shell, name: tpl-b, cmd: "echo b"}\n'
    )
    spec_file.write_text(spec_file.read_text() + "  templ:\n    use: base\n    description: alt\n")
    return spec_file


def test_graph_use_loop_does_not_block_save(page, fabrikant_home):
    """Eine `use:`-Template-Loop (keine steps in der rohen Spec) blockiert den
    Save nicht — die Client-Validierung 'mindestens ein Step' gilt nicht für
    Template-Loops, und die Loop überlebt den Save unverändert."""
    spec_file = _add_template_loop(fabrikant_home)
    _open_graph(page)
    # Projekt-Karte: description ändern → save (würde ohne Fix an der
    # Step-Validierung der templ-Loop scheitern).
    _click_node(page, "proj:uitest")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    page.fill("#graphnode input[data-gn='description']", "mit Template-Loop")
    page.click("#graphnode button:has-text('speichern')")
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=5000)
    text = spec_file.read_text()
    assert "description: mit Template-Loop" in text
    assert "use: base" in text  # Template-Loop bleibt erhalten


def test_graph_use_loop_edit_keeps_template_steps(page, fabrikant_home):
    """Loop-Karte einer `use:`-Loop: kein "+ step", geerbte Steps als Hinweis;
    description ändern → save schreibt KEIN `steps: []`/`triggers: []` (das
    würde die Template-Steps/-Triggers überschreiben)."""
    spec_file = _add_template_loop(fabrikant_home)
    _open_graph(page)
    _open_card(page, "loop:uitest:templ")
    card = page.locator("#graphnode")
    assert card.locator("button:has-text('+ neuer Step')").count() == 0
    assert "tpl-a, tpl-b" in card.inner_text()
    page.fill("#graphnode input[data-gn='description']", "neu")
    page.click("#graphnode button:has-text('speichern')")
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=5000)
    templ = spec_file.read_text().split("  templ:")[1]
    assert "description: neu" in templ
    assert "steps" not in templ and "triggers" not in templ
    loop = specs.load_spec(spec_file)["loops"]["templ"]
    assert [s["name"] for s in loop["steps"]] == ["tpl-a", "tpl-b"]
    assert loop["triggers"] == [{"type": "cron"}]


def test_graph_use_loop_with_editor_saves(page, fabrikant_home):
    """Loop-Karte einer `use:`-Loop: Template-Parameter (with:) sind editierbar —
    Felder aus dem Template-params-Block (Default + Pflichtfeld), save schreibt
    den with:-Wert in die Spec."""
    spec_file = fabrikant_home / "projects" / "uitest.fabrikant.yaml"
    tpl_dir = fabrikant_home / "projects" / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "dev.loop.yaml").write_text(
        "params:\n"
        '  issue_label: "ready"\n'
        "  branch: null\n"
        "loop:\n"
        "  triggers:\n"
        "    - {type: cron}\n"
        "  steps:\n"
        '    - {type: shell, name: d1, cmd: "echo d"}\n'
    )
    # branch ist Pflichtfeld (null-Default) → Spec braucht ein with: (sonst
    # defekte Spec, Save blockiert).
    spec_file.write_text(spec_file.read_text() + "  devloop:\n    use: dev\n    with:\n      branch: main\n")
    _open_graph(page)
    _open_card(page, "loop:uitest:devloop")
    card = page.locator("#graphnode")
    # with:-Felder werden asynchron aus dem Template-params-Block geladen.
    page.wait_for_function("document.querySelector('#graphnode input[data-gw=\"issue_label\"]') !== null", timeout=5000)
    assert card.locator("input[data-gw='issue_label']").input_value() == "ready"  # Template-Default
    assert card.locator("input[data-gw='branch']").input_value() == "main"  # aus with:
    card.locator("input[data-gw='issue_label']").fill("in progress")
    page.click("#graphnode button:has-text('speichern')")
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=5000)
    text = spec_file.read_text()
    assert "issue_label: in progress" in text
    assert "branch: main" in text
    assert "use: dev" in text  # Template-Loop bleibt erhalten


def test_graph_use_loop_drilldown_shows_inherited_steps(page, fabrikant_home):
    """Drill-down in eine `use:`-Loop zeigt die Template-Steps read-only
    (`tstep`) und keinen "+ New Step"-Ghost; Kanten-Label = Template-Trigger."""
    _add_template_loop(fabrikant_home)
    _open_graph(page)
    label = page.evaluate(f"{_cy(page)}.edges('[target=\"loop:uitest:templ\"]').data('label')")
    assert label == "cron"
    _click_node(page, "loop:uitest:templ")
    page.wait_for_function(f"() => {_cy(page)}.nodes('[kind=\"tstep\"]').length === 2", timeout=5000)
    assert page.evaluate(f"{_cy(page)}.nodes('[id=\"ghost:step\"]').length") == 0


def test_graph_use_loop_insert_shows_all_inherited_steps(page, fabrikant_home):
    """Drill-down in eine `use:`-Loop mit `insert:` zeigt ALLE expandierten
    Template-Steps als Kette ab Loop-Wurzel — auch den ERSTEN (nicht nur die
    nach dem Insert)."""
    spec_file = fabrikant_home / "projects" / "uitest.fabrikant.yaml"
    tpl_dir = fabrikant_home / "projects" / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "chain.loop.yaml").write_text(
        "params: {}\nloop:\n  triggers:\n    - {type: cron}\n  steps:\n"
        '    - {type: shell, name: first, cmd: "echo 1"}\n'
        '    - {type: shell, name: second, cmd: "echo 2"}\n'
        '    - {type: shell, name: third, cmd: "echo 3"}\n'
    )
    spec_file.write_text(
        spec_file.read_text()
        + "  chained:\n    use: chain\n"
        + "    insert:\n      - after: second\n        steps:\n"
        + '          - {type: shell, name: inserted, cmd: "echo x"}\n'
    )
    _open_graph(page)
    _click_node(page, "loop:uitest:chained")
    # 4 Steps: first, second, inserted, third — der ERSTE (first) muss dabei sein.
    page.wait_for_function(f"() => {_cy(page)}.nodes('[kind=\"tstep\"]').length === 4", timeout=5000)
    ids = page.evaluate(f"{_cy(page)}.nodes('[kind=\"tstep\"]').map(n => n.id())")
    assert sorted(ids) == [
        "tstep:uitest:chained:0",
        "tstep:uitest:chained:1",
        "tstep:uitest:chained:2",
        "tstep:uitest:chained:3",
    ]
    # Kette ab Loop-Wurzel: nur der erste Step hat eine Kante zur Loop.
    edges = page.evaluate(f"() => {_cy(page)}.edges().map(e => [e.data('kind'), e.data('source'), e.data('target')])")
    assert [e for e in edges if e[0] == "has_step"] == [
        ["has_step", "loop:uitest:chained", "tstep:uitest:chained:0"],
    ]


def test_graph_drilldown_selects_exact_loop_steps(page, fabrikant_home):
    """Drill-down auf `lint` zieht nicht die Steps von `lint:x` mit
    (Kinder per Kanten — has_step + next_step-Kette — nicht per ID-Präfix)."""
    spec_file = fabrikant_home / "projects" / "uitest.fabrikant.yaml"
    spec_file.write_text(
        spec_file.read_text()
        + '  "lint:x":\n    triggers:\n      - {type: manual}\n'
        + '    steps:\n      - {type: shell, name: "other", cmd: "echo x"}\n'
    )
    _open_graph(page)
    _drill_into_lint(page)
    ids = page.evaluate(f"{_cy(page)}.nodes('[kind=\"step\"]').map(n => n.id())")
    assert sorted(ids) == ["step:uitest:lint:0", "step:uitest:lint:1"]


def test_graph_ctx_state_button_closes_menu(page):
    """State-Buttons im Kontextmenü schließen es (expliziter `before`-Hook
    von stateButtons statt Markup-Patching)."""
    _open_graph(page)
    _rightclick_node(page, "loop:uitest:lint")
    page.wait_for_function("!document.querySelector('#graphctx').classList.contains('hidden')", timeout=3000)
    page.on("dialog", lambda d: d.accept(""))  # Auto-Resume-Prompt: leer = unbegrenzt
    page.click("#graphctx button:has-text('pausieren')")
    page.wait_for_function("document.querySelector('#graphctx').classList.contains('hidden')", timeout=3000)


def test_graph_llm_temperature_keeps_float(page, fabrikant_home):
    """Die llm-Step-`temperature` ist ein Float — Editieren darf sie nicht auf
    eine Ganzzahl truncieren (0.7 → 0)."""
    spec_file = fabrikant_home / "projects" / "uitest.fabrikant.yaml"
    spec_file.write_text(
        spec_file.read_text().replace(
            '      - {type: shell, name: "fix", cmd: "echo fix"}',
            '      - {type: shell, name: "fix", cmd: "echo fix"}\n'
            '      - {type: llm, name: "ask", prompt: "hi", temperature: 0.7}',
        )
    )
    _open_graph(page)
    _drill_into_lint(page)
    _click_node(page, "step:uitest:lint:2")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    # Feld zeigt den Float-Wert (nicht 0).
    assert page.locator("#graphnode input[data-ge='temperature']").input_value() == "0.7"
    # Ändern → save: Datei bekommt den Float-Wert.
    page.locator("#graphnode input[data-ge='temperature']").fill("0.3")
    page.click("#graphnode button:has-text('speichern')")
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=5000)
    text = spec_file.read_text()
    assert "temperature: 0.3" in text


def test_graph_card_fits_and_is_resizable(page):
    """Karten-Layout: Formularfelder sprengen die Karte nicht (kein
    horizontales Scrollen, Labels nicht abgeschnitten); die Karte ist per
    CSS-resize in der Größe änderbar."""
    _open_graph(page)
    _open_card(page, "loop:uitest:lint")  # Loop-Karte mit verschachtelten Step-Karten
    dims = page.evaluate(
        """() => { const b = document.querySelector('#graphnode .graphnode-body');
                   return [b.scrollWidth, b.clientWidth]; }"""
    )
    assert dims[0] <= dims[1], f"Karte läuft horizontal über: {dims}"
    assert page.evaluate("getComputedStyle(document.querySelector('#graphnode')).resize") == "both"


def _add_relay_block(fabrikant_home):
    """Spec um einen `relay:`-Block erweitern (erzeugt drei Loops beim Expandieren)."""
    spec_file = fabrikant_home / "projects" / "uitest.fabrikant.yaml"
    spec_file.write_text(
        spec_file.read_text().replace(
            "loops:\n", "github: o/r\nrelay:\n  allowed: [me]\n  bot_login: my-builder-app\nloops:\n", 1
        )
    )
    return spec_file


RELAY_LOOPS = ("relay-dispatch", "relay-session", "relay-audit")


def test_graph_shows_generated_relay_loops_readonly(page, fabrikant_home):
    """Loops aus dem `relay:`-Block stehen in keiner rohen Spec — der Graph zeigt
    sie trotzdem (gestrichelt, read-only) mit Trigger-Label und Drill-down."""
    _add_relay_block(fabrikant_home)
    _open_graph(page)
    gl = page.evaluate(f"() => {_cy(page)}.nodes('[kind=\"gloop\"]').map(n => n.data('label')).sort()")
    assert gl == sorted(RELAY_LOOPS)
    label = page.evaluate(f"{_cy(page)}.edges('[target=\"loop:uitest:relay-dispatch\"]').data('label')")
    assert label == "cron"
    _click_node(page, "loop:uitest:relay-session")
    page.wait_for_function(f"() => {_cy(page)}.nodes('[kind=\"tstep\"]').length > 0", timeout=5000)
    assert page.evaluate(f"{_cy(page)}.nodes('[id=\"ghost:step\"]').length") == 0
    assert page.evaluate(f"{_cy(page)}.nodes('[kind=\"step\"]').length") == 0


def test_graph_generated_loop_context_menu_has_no_edit(page, fabrikant_home):
    """Rechtsklick auf eine generierte Loop: run + pause, aber kein edit
    (Quelle ist der `relay:`-Block der YAML)."""
    _add_relay_block(fabrikant_home)
    _open_graph(page)
    _rightclick_node(page, "loop:uitest:relay-dispatch")
    page.wait_for_function("!document.querySelector('#graphctx').classList.contains('hidden')", timeout=3000)
    menu = page.locator("#graphctx")
    assert menu.locator("button:has-text('starten')").count() == 1
    assert menu.locator("button:has-text('pausieren')").count() == 1
    assert menu.locator("button:has-text('bearbeiten')").count() == 0


def test_graph_save_does_not_write_generated_loops(page, fabrikant_home):
    """Save mit generierten Loops im Graph: die YAML behält den `relay:`-Block und
    bekommt keine relay-*-Loops (sie bleiben generiert)."""
    spec_file = _add_relay_block(fabrikant_home)
    _open_graph(page)
    _click_node(page, "proj:uitest")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    page.fill("#graphnode input[data-gn='description']", "mit relay")
    page.click("#graphnode button:has-text('speichern')")
    page.wait_for_function("document.querySelector('#graphnode').classList.contains('hidden')", timeout=5000)
    raw = specs.load_spec_raw(spec_file)
    assert raw["description"] == "mit relay"
    assert raw["relay"]["bot_login"] == "my-builder-app"
    assert not set(RELAY_LOOPS) & set(raw["loops"])
    assert set(RELAY_LOOPS) <= set(specs.load_spec(spec_file)["loops"])


def test_graph_generated_loop_details_card(page, fabrikant_home):
    """Rechtsklick auf eine generierte Loop → "ⓘ details": read-only Karte mit
    Herkunft, Triggers und Steps — ohne Eingabefelder und ohne save."""
    _add_relay_block(fabrikant_home)
    _open_graph(page)
    _rightclick_node(page, "loop:uitest:relay-dispatch")
    page.wait_for_function("!document.querySelector('#graphctx').classList.contains('hidden')", timeout=3000)
    page.click("#graphctx button:has-text('Details')")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    card = page.locator("#graphnode")
    text = card.inner_text()
    assert "relay-dispatch" in text and "read-only" in text and "relay:" in text
    assert "cron" in text
    assert card.locator("input, select, textarea").count() == 0
    assert card.locator("button:has-text('speichern')").count() == 0


def test_graph_generated_step_click_opens_readonly_card(page, fabrikant_home):
    """Klick auf einen Step einer generierten Loop öffnet dessen Details read-only
    (Typ + alle Parameter, z. B. prompt_file des Session-Agents)."""
    _add_relay_block(fabrikant_home)
    _open_graph(page)
    _click_node(page, "loop:uitest:relay-session")
    page.wait_for_function(f"() => {_cy(page)}.nodes('[kind=\"tstep\"]').length > 0", timeout=5000)
    _wait_graph_settled(page, "tstep:uitest:relay-session:0")
    labels = page.evaluate(f"() => {_cy(page)}.nodes('[kind=\"tstep\"]').map(n => n.id())")
    agent_id = next(i for i in labels if page.evaluate(f"{_cy(page)}.getElementById('{i}').data('label')") == "session")
    _click_node(page, agent_id)
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    card = page.locator("#graphnode")
    text = card.inner_text()
    assert "read-only" in text and "agent" in text and "prompt_file" in text
    assert card.locator("input, select, textarea").count() == 0


def test_graph_template_step_click_opens_readonly_card(page, fabrikant_home):
    """Auch geerbte Template-Steps (`use:`) bekommen die read-only Detailkarte."""
    _add_template_loop(fabrikant_home)
    _open_graph(page)
    _click_node(page, "loop:uitest:templ")
    page.wait_for_function(f"() => {_cy(page)}.nodes('[kind=\"tstep\"]').length === 2", timeout=5000)
    _wait_graph_settled(page, "tstep:uitest:templ:0")
    _click_node(page, "tstep:uitest:templ:0")
    page.wait_for_function("!document.querySelector('#graphnode').classList.contains('hidden')", timeout=3000)
    text = page.locator("#graphnode").inner_text()
    assert "tpl-a" in text and "vom Template" in text and "echo a" in text
