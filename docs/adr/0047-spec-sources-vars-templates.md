# ADR 0047: Spec-Quellen, Pfadauflösung, `vars` und Loop-Templates

**Status:** Akzeptiert (2026-10-26, Merge #329; vorgeschlagen 2026-10-23, 2026-10-25 Nummer 0041 → 0047 wegen Kollision mit offenem PR #332, Hinweis zum `scripts/`-Fallback; 2026-11-03 Nachtrag: Run-Worktrees außerhalb des Quell-Checkouts)
**Herkunft:** Issue „Specs und Prompts außerhalb des Fabrik-Repos“ (Portabilität). Verwandt: Datenverzeichnis-Entscheidung (Nr. 0016, noch nicht gemergt;
`FABRIK_DATA` wird hier minimal als `config.DATA_DIR` eingeführt), ADR 0001 (Dogfooding).

## Kontext

Specs lagen fest in `FABRIK_HOME/projects`, `prompt_file` wurde relativ zu `FABRIK_HOME` aufgelöst, und
`fabrik.fabrik.yaml` nannte den Repo-Namen 24-mal. Private Projekte mischten sich mit dem Tool-Code; ein
Fork oder eine zweite Instanz musste Specs committen oder suchen-und-ersetzen.

## Entscheidung

1. **Spec-Quellen** (erster Treffer gewinnt):
   1. `FABRIK_PROJECTS_PATH` — doppelpunkt-getrennt wie `PATH`; jeder Eintrag ist ein Ordner mit
      `*.fabrik.yaml|json` oder ein Repo mit `.fabrik/fabrik.yaml`;
   2. `FABRIK_DATA/projects` (`config.PROJECTS_DIR`; ohne `FABRIK_DATA` = `FABRIK_HOME`, also wie bisher);
   3. per `fabrik project add <pfad>` registrierte Zielrepos (`state/registered-projects.json`), deren
      `.fabrik/fabrik.yaml` automatisch gefunden wird;
   4. mitgelieferte Beispiele `FABRIK_HOME/projects`.

   Dieselbe `name:` in zwei Quellen ist ein Fehler: die höhere Quelle gewinnt zur Laufzeit,
   `fabrik validate` meldet jede verdeckte Spec und endet mit Exit 1. Spec im Zielrepo: ohne `name:` gilt der
   Repo-Ordner, ohne `root:` das Repo selbst.
2. **Pfade relativ zur Spec-Datei:** `prompt_file` (agent, review) und `scripts/…`-Referenzen in
   Shell-Befehlen werden erst relativ zur Spec aufgelöst, dann relativ zu `FABRIK_HOME` (mitgelieferte
   Prompts und Skripte, Rückwärtskompatibilität); absolute Pfade bleiben. Shell-Befehle behalten dabei
   den Projekt-Worktree als `cwd`; nur vorhandene `scripts/…`-Pfade werden im Befehl absolut eingesetzt.
3. **`vars:`** (Top-Level-Mapping): `{name}` wird beim Laden in allen Strings der Spec ersetzt. Besteht ein
   String nur aus `{name}`, bleibt der Typ erhalten (Liste, Zahl). Nur Schlüssel aus `vars` werden ersetzt;
   alle übrigen `{x}` bleiben Laufzeit-Variablen. Namen der Laufzeit-Variablen (`project`, `root`,
   `issue_number`, …) sind als `vars`-Schlüssel verboten. `connection:`-Referenzen (#156) sind Folgearbeit.
4. **Loop-Templates:** `templates/<name>.loop.yaml` (Suche: `<spec-ordner>/templates`, `FABRIK_DATA/templates`,
   `FABRIK_HOME/templates`) mit `params:` (Defaults; `null` = Pflicht) und `loop:`. Ein Projekt bindet per
   `use: <name>` und `with: {…}` ein. Rangfolge der Parameter: `with` > Spec-`vars` > Template-Default. Weitere
   Loop-Schlüssel (`description`, `triggers`, `steps`, …) überschreiben die des Templates; `insert:
   [{after: <step>, steps: […]}]` fügt Steps hinter benannte Template-Steps ein. Mitgeliefert: `develop`,
   `review-pr`. Expansion läuft vor `vars`. Wir nutzen nur `use:` (kein zusätzliches `extends:`).
5. **Prompts:** `prompts/fabrik-*.prompt.md` bleiben projektspezifisch (Instanz-Parameter `prompt_file`);
   generisch ist die Pipeline (Template), nicht der Prompt-Text.

## Konsequenzen

- Zweites Projekt: 5–10 Zeilen Spec (siehe README). `fabrik.fabrik.yaml` ist eine Instanz der Templates.
- Private Specs/Prompts liegen nicht
  mehr im Repo; Migration im README.
- Der Spec-Hash eines Runs deckt nur die Spec-Datei ab, nicht das Template.
- Eine Spec aus `FABRIK_PROJECTS_PATH`/Zielrepo kann Shell-Steps enthalten: Quellen sind vertrauenswürdig
  zu halten wie `FABRIK_HOME/projects`.
- Der `FABRIK_HOME`-Fallback für `scripts/…` kann bei Namensgleichheit ein Fabrik-Skript treffen, wo ein Zielrepo
  sein eigenes Worktree-Skript meint (fehlt `scripts/x` neben der Spec, existiert aber in der Installation).
  Skript-Namen zwischen Installation und Zielrepos eindeutig halten.

## Nachtrag 2026-11-03: Run-Worktrees außerhalb des Quell-Checkouts

**Kontext.** Die Worktrees pro Run (ADR 0028) lagen unter `<repo>/.fabrik/worktrees` —
außerhalb von `fabrik/` (OpenCode lädt `AGENTS.md` vom Workdir aufwärts), aber im Repo.
Eine IDE mit dem Repo als Projekt indiziert damit jeden Run-Checkout samt `.venv`. Am
2026-11-02 lief dabei der IntelliJ-Heap über (`OutOfMemoryError`, 2 GB Heap-Dump), der
Rechner swappte (29 GB RAM + 7,9 GB Swap) und musste neu gestartet werden. Dasselbe
Prinzip wie oben für Specs gilt für Laufzeitdaten: sie gehören nicht in den Tool-Checkout.

**Entscheidung.**
1. `config.WORKTREES_DIR` = `FABRIK_WORKTREES`, sonst
   `$XDG_STATE_HOME/fabrik/worktrees/<install_id>` (ohne `XDG_STATE_HOME`:
   `~/.local/state/…`). XDG-State statt Cache: Worktrees pausierter Runs müssen Neustarts
   überleben.
2. `<install_id>` = `<Ordnername von FABRIK_HOME>-<8 Hex sha256 des Pfads>`. Zwei Checkouts
   mit gleich benannten Projekten teilen sich sonst eine Wurzel, und
   `cleanup_orphaned_worktrees` des einen räumt die laufenden Runs des anderen ab.
   `FABRIK_WORKTREES` wird unverändert genutzt (eine Installation pro Pfad bleibt Sache des
   Betreibers).
3. Übergang: `<repo>/.fabrik/worktrees` bleibt als Legacy-Wurzel lesbar. Ein dort
   vorhandener Run-Worktree gewinnt beim Resume; der Start-Cleanup durchsucht beide Wurzeln.
   Neue Runs entstehen nur unter der neuen Wurzel; die alte läuft leer.

**Konsequenzen.**
- IDEs, Indexer und File-Watcher des Repos sehen keine Run-Checkouts mehr.
- Welche Instruktionen Agents laden, ändert sich nicht: Instruktionsdateien kommen nur als
  versionierte Dateien des Zielrepos in den Worktree; ungetrackte fehlen dort an beiden Orten.
  Wie bisher liegt kein Verzeichnis des Fabrik-Pakets (dessen `AGENTS.md`/`CLAUDE.md`) auf dem
  Weg nach oben, so dass Agents keine Instruktionen des Haupt-Checkouts mitladen.
- `fabrik/CLAUDE.md` (`@AGENTS.md`) ist versioniert: Claude Code liest kein `AGENTS.md`, ohne
  die Datei bekämen `claude_code`-Agents in Fabrik-Runs die Regeln aus `AGENTS.md` nicht.
- Container/systemd: `FABRIK_WORKTREES` auf ein persistentes Volume setzen, wenn `$HOME`
  flüchtig ist.
- Die Legacy-Wurzel samt Lookup kann entfallen, sobald keine Installation mehr pausierte
  Runs von vor 2026-11 hat.
