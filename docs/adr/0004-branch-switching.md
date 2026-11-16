# ADR 0004: Branch-Wechsel zur Laufzeit (UI)

**Status:** Akzeptiert (2026-10-17); UI-Selector entfernt (2026-11-04) — API und Override-State bleiben
**Entscheider:** Dominic
**Kontext:** Die Ziel-Branch eines Projekts ist in der Spec fixiert
(`repo.branch`). Ein Branch-Wechsel (z. B. `fabrik` von `main` auf
`fabrik/v0.3`) erforderte ein manuelles YAML-Edit; die Web-UI zeigte den
Branch nur read-only an.

## Entscheidung

Branch-Override als Laufzeit-State, **keine** Spec-Änderung:

- `state/branch-overrides.json`: `{projekt: branch}` — kleine JSON-Datei in
  `STATE_DIR` (gleiches Muster wie `state/active_llm.json`).
- `specs.load_projects()` legt den Override am Ende auf die geladene Spec
  (`repo.branch`) — alle Konsumenten (Server, Engine, CLI, Scheduler) sehen
  denselben Branch an einer einzigen Stelle.
- Die YAML bleibt der kanonische Default; die Datei (inkl. Comments) wird nie
  umgeschrieben.
- API:
  - `GET /api/projects/<name>/branches` → `{branches: [...], current: "…"}`
    (lokale + Remote-Branches, dedupliziert, sortiert)
  - `POST /api/projects/<name>/branch` `{"branch": "…"}` → validiert, dass der
    Branch im Repo existiert (sonst 400), persistiert den Override
- UI: pro Projekt ein Branch-Selector in der Detail-View (Dropdown,
  `#llmselect`-Stil).

Der Worktree synchronisiert beim nächsten Run auf `origin/<branch>`
(`gitops.ensure_worktree`) — kein zusätzlicher Mechanismus nötig.

## Begründung

- **Warum nicht die YAML umschreiben?** `yaml.dump` zerstört Comments; die
  Spec-Dateien sind stark kommentiert. Eine separate State-Datei hält die
  Spec als Single Source of Truth für den Default und macht den Override
  sichtbar, editierbar und pro Deployment lokal.
- **Warum beim Load anwenden, nicht im Worktree?** Alle Konsumenten lesen den
  Branch aus der Spec; ein einzelner Anwendungspunkt in `load_projects()`
  garantiert Konsistenz (kein Zweitspeicher, den Engine/Scheduler verpassen
  könnten).
- **Validierung:** Die POST-Route lehnt nicht existierende Branches ab (400) —
  ein Tippfehler bricht nicht erst den nächsten Run.
- **Reset:** Eintrag (oder die Datei) löschen → Projekt fällt auf den
  Spec-Default zurück.
- **Kein Security-Impact:** Die Endpunkte liegen hinter dem bestehenden
  `FABRIK_TOKEN`-Gate wie alle anderen POST-Routen; der Branch-Wechsel selbst
  ist read-only gegenüber dem Remote (nur `fetch` + detached Checkout).

## Änderung 2026-11-04

Der Branch-Selector in der Detail-View ist entfernt (kein praktischer Nutzen; stiller
per-Deployment-Override). Der Branch wird weiter read-only in der Projektliste angezeigt.
Endpunkte und `state/branch-overrides.json` bleiben unverändert bestehen.
