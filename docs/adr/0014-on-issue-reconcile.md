# ADR 0014: Reconcile-Modus für den `on_issue`-Trigger

**Status:** Akzeptiert (2026-10-19); Amendment 2026-10-28 (ADR 0036 umgesetzt):
Auto-Retry ist über die Retry-Policy (ADR 0036, Entscheidung 7) möglich — der
Abschnitt „Kein Auto-Retry“ gilt für `reconcile` selbst unverändert
**Entscheider:** Fabrik (dogfooding, Issue #131)
**Kontext:** Der `on_issue`-Trigger merkte sich gesehene Issues nur im
Speicher. Der erste Poll nach jedem Server-Start markierte alle offenen Issues
als gesehen, ohne zu feuern. Jedes Issue, das beim (Neu-)Start schon existierte
oder während einer Downtime entstand, wurde nie verarbeitet — u. a. die
Refactor-Issues #63–#65 aus dem `refactor-sweep` und das Roadmap-Backlog
#16–#34. Zusätzlich sah der Poll nur die 50 neuesten Issues.

## Entscheidung

`on_issue` bekommt — wie `on_pr` (ADR 0011) — die opt-in-Option
`reconcile: true` (`src/fabrik/scheduler.py`):

- **Persistente Quelle statt In-Memory-Set:** Der Trigger feuert für jedes
  passende offene Issue, für das der Loop **noch nie** einen Run hatte —
  geprüft über die Run-Records (`runs/<projekt>/*.json`, `loop` +
  `trigger == "on_issue:#N"`). Das überlebt Restarts und arbeitet den Bestand ab.
- **Kein Auto-Retry:** „Hatte schon einen Run" zählt unabhängig vom Status
  (auch `error`/`fail`) — `reconcile` startet für einen Trigger nie einen
  zweiten Run, damit dauerhaft kaputte Issues keine Endlosschleife drehen.
  Automatisches Retry existiert ausschließlich als begrenzte, klassenbasierte
  Retry-Policy (ADR 0036, Entscheidung 7); „Retry bleibt manuell" entfällt
  dadurch.
- **Drosselung:** höchstens `max_per_poll` neue Runs pro Poll und Loop
  (Default **2**, Werte < 1 werden auf 1 angehoben), **älteste Issues zuerst**
  (`sort=created&direction=asc`). Ohne Drosselung würde `refine` beim
  Aktivieren ~25 Runs mit je einem Approval-Gate auf einmal starten.
- **Pagination:** Im Reconcile-Modus werden bis zu 10 Seiten à 50 Issues
  gelesen (lazy — nur so viele, bis `max_per_poll` Kandidaten gefunden sind),
  damit verarbeitete alte Issues neuere nicht dauerhaft verdecken.
- **Gemeinsamer Run-Lookup:** `loop_run_triggers(projekt, loop, statuses)`
  ist der eine Helfer für Run-Lookup nach Trigger; `_has_active_pr_run`
  (on_pr) nutzt ihn mit `ACTIVE_RUN_STATUSES`.
- Ohne `reconcile` bleibt das Event-Verhalten unverändert.

Aktiviert in `projects/fabrik.fabrik.yaml` für `refine` und `develop`.

## Konsequenzen

- Ein Run, der vom Projekt-Lock (`_safe_run`, non-blocking) verworfen wird,
  hinterlässt keinen Record — der nächste Poll feuert erneut. Reconcile macht
  verlorene Trigger-Events damit selbstheilend.
- Wird ein Run-Record gelöscht, feuert das Issue erneut (bewusst: so lässt
  sich ein Retry auch ohne UI erzwingen).
- Jeder Poll liest die Run-Records des Projekts einmal (O(Runs)); bei 300 s
  Poll-Intervall unkritisch. Wächst `runs/` stark, ist ein Index der nächste
  Schritt.
- Ein Issue, das *nachträglich* das Label bekommt, feuert im Reconcile-Modus
  (wie im Event-Modus).
