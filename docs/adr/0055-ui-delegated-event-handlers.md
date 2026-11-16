# ADR 0055: UI ohne Inline-Event-Handler (data-* + Delegation)

**Status:** Akzeptiert (2026-11-13)
**Baut auf:** ADR 0030 (Auth/Rollen), ADR 0038 (Run-Modal).

## Kontext

Die Web-UI (`web/app.js`) baute ~34 Inline-Event-Handler (`onclick="…"`,
`onchange="…"`) in dynamisch gerenderte Markup-Strings ein. Inline-Handler sind
ein bekannter XSS-Vektor: jeder Wert, der in den Handler-String interpoliert
wird, muss manuell maskiert werden — ein vergessenes `esc()` ist sofort
ausführbarer Code. Zusätzlich erzwingen Inline-Handler `unsafe-inline` in der
CSP und sind schwerer zu auditieren. Projekt-/Loop-/Run-Namen mit
Anführungszeichen oder `<` (z. B. aus Dateinamen) sind ein realistischer
Auslöser.

## Entscheidung

1. **Kein Inline-JS mehr in `web/`.** Alle Handler werden per
   `addEventListener`/Delegation gebunden; die Werte stehen in `data-*`-Attributen
   (entity-maskiert via `esc()`). `data-*`-Attribute sind Daten, kein Code.
2. **Delegation auf statischen Containern.** Die Container (`#projects`,
   `#detail`, `#graphctx`, `#runactions`, `#schedpanel`) sind statisch in
   `index.html`; je ein `click`/`keydown`-Listener reicht für alle dynamisch
   gerenderten `[data-act]`-Elemente. Der jeweils **innerste** `[data-act]`-Eltern
   des Klickziels gewinnt — das ersetzt die früheren
   `event.stopPropagation()`-Aufrufe (Button in klickbarer Zeile).
3. **`[data-stop]`** schluckt Klicks ohne Aktion (z. B. der gesperrte ▶ run in
   der klickbaren Loop-Karte).
4. **`data-act`-Vokabular** (stabile Strings, kein Code): `select-project`,
   `toggle-approvals`, `open-run`, `toggle-shelved`, `shelved-retry`,
   `shelved-dismiss`, `set-state`, `toggle-spec`, `toggle-graph`,
   `toggle-loop-spec`, `start-run`, `graph-save`, `graph-discard`, `close-card`,
   `card-add-step`, `card-add-trigger`, `card-remove-loop`, `card-move-step`,
   `card-remove-step`, `step-move`, `step-remove`, `trigger-remove`, `ctx-run`,
   `ctx-edit`, `run-approve`, `run-reject`, `sched-run-now`, `sched-ot-open`,
   `sched-ot-cancel`, `sched-ot-submit`, `sched-ot-close`.
5. **Regressions-Schutz:** `tests/test_web_no_inline_handlers.py` scannt `web/`
   (ohne `vendor/`) und scheitert bei jedem `on*`-Attribut mit String-Wert;
   `tests/test_ui_browser.py` prüft, dass Namen mit `'`, `"`, `<` gerendert,
   klickbar und code-frei sind.

## Konsequenzen

- Verhalten bleibt identisch (inkl. `stopPropagation`-Semantik für Buttons in
  klickbaren Zeilen); die UI ist CSP-freundlicher (`unsafe-inline` entfällt).
- Neue interaktive Elemente folgen dem Muster: `data-act` + `data-*`-Werte,
  Handler in der Delegation — kein Inline-JS.
- `stateButtons()` verliert das `{ before }`-Parameter; das Kontextmenü schließt
  sich über `onGraphCtxClick` (vor `set-state`), `ctx-run`/`ctx-edit` schließen
  sich selbst.
