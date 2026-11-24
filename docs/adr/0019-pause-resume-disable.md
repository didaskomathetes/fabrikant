# ADR 0019: Pause/Resume/Disable pro Projekt und Loop

**Status:** Akzeptiert (2026-10-20, Merge #207; vorgeschlagen 2026-10-20); Amendment 2026-10-25 (PR #326): eingereihte, noch nicht gestartete Runs werden zur Ausführungszeit erneut geprüft; geändert 2026-10-23: Worktree-Lease statt Single-Process-Garantie, #237/PR #328

## Kontext

Bei Fehlerkaskaden (z. B. `review-pr` failt alle 5 Minuten) oder Wartung gab es
keinen Schalter außer Spec-Edit oder Server-Stopp.

## Entscheidung

- Zustand `active` | `paused` | `disabled`, optional `until` (Auto-Resume), pro Projekt und pro Loop.
- `paused`: Scheduler-Trigger (cron/on_file/on_issue/on_pr) feuern und pollen nicht; manuelle Runs erlaubt.
  `disabled`: zusätzlich keine manuellen Runs (UI/CLI/Chat/API). Laufende Runs bleiben unberührt.
- Projekt-Zustand schlägt Loop-Zustand (Projekt != `active` → gilt für alle Loops).
- Persistenz in `state/run-states.json` (atomar geschrieben), **nicht** in der Spec → überlebt Restarts und Spec-Änderungen.
  Nur `paused`/`disabled` werden gespeichert; `active` = kein Eintrag.
- `until` wird zu jedem Scheduler-Tick geprüft (`resume_expired`); abgelaufene Einträge zählen sofort als `active`.
- Audit-Log `state/run-state-changes.jsonl` (JSONL: ts, actor, project, loop, from, to, until) + stdout.
- Schnittstellen: `POST /api/projects/<p>/state` (`{state, loop?, until?}`), CLI `fabrikant pause|resume|disable`, Chat, UI-Buttons.

**Amendment (PR #326):** Die Prüfung von Pause/Disable erfolgt nicht nur beim
Einreihen (Scheduler bzw. UI/CLI/Chat/API), sondern erneut zur Ausführungszeit:
Trigger-Runs, die im Worker-Pool-Queue warten, werden verworfen, wenn der Loop
inzwischen `paused`/`disabled` ist; wartende manuelle Runs (inkl. Resume) werden
verworfen, wenn der Loop inzwischen `disabled` ist (queued-Record → `error`,
Resume bleibt `needs_approval` und kann nach Resume erneut freigegeben werden).
Laufende Runs bleiben weiterhin unberührt.

## Worktree-Lease und Single-Process-Garantie

Jeder Run-Worktree hat eine Lease-Datei `<WORKTREES_DIR>/<projekt>/<run_id>.lease`
(JSON: `pid`, `heartbeat`, `token`; außerhalb des Git-Trees). Sie wird vor dem Anlegen des
Worktrees geschrieben, per Heartbeat-Thread alle `config.WORKTREE_LEASE_HEARTBEAT_S` (30 s)
erneuert und bei Run-Ende, Pause und Worktree-Entfernung gelöscht.
`cleanup_orphaned_worktrees` überspringt einen Worktree, dessen Lease von einem
**anderen** Prozess stammt, dessen PID lebt und dessen Heartbeat jünger als
`config.WORKTREE_LEASE_TIMEOUT_S` (300 s) ist. Fehlende/korrupte Lease, eigene PID, tote
PID oder abgelaufener Heartbeat schützen nicht; `needs_approval` schützt weiterhin.

Damit löscht ein zweiter Prozess (Server, `fabrikant run`/`approve`) keine Worktrees
laufender Runs mehr. **Weiterhin offen** und nicht unterstützt: Slots (`max_parallel`),
Dedup und Vorrang-Warter (`runs.claim_run`) sind prozesslokal; parallele Prozesse auf
demselben Projekt können das Slot-Limit überschreiten und denselben Trigger doppelt
starten. Prozessübergreifende Slots/Dedup sind ein Folge-Thema (#280). Einschränkung: PID-Prüfung
gilt nur auf demselben Host. Details zur Lease-Implementierung stehen in ADR 0028.

## Specs ohne `repo:`

Specs ohne `repo:` arbeiten ohne Isolation direkt im `root`; parallele Runs würden
sich dort über Git-Index und Dateien überlappen. Daher gilt für diese Specs fest
`max_parallel: 1`; ein höherer Wert in der Spec wird ignoriert. `worktree: false`-
Loops sind davon ausgenommen, da sie ein eigenes Scratch-Workdir verwenden.

**Known Gap:** Das Problem existiert nur bei geteiltem Dateisystem
(Single-Machine/Homelab-Betrieb). Bei container-basierten Runs (Sandbox pro Run
mit eigenem Checkout, vgl. #20) löst es sich auf; der Sync-back von Änderungen
für repo-less Specs bleibt dort offen.

## Konsequenzen / Nicht-Ziele

- Während einer Pause verpasste Cron-Minuten werden nicht nachgeholt; `on_issue` im Event-Modus
  initialisiert beim ersten Poll nach Resume neu, `reconcile`-Trigger arbeiten den Bestand ab.
- Rollen (`operator`, #159) existieren noch nicht: Zugriff = bestehende Origin-/`FABRIKANT_TOKEN`-Prüfung (admin-Fallback).
  Der `actor` im Audit-Log ist `api:<client-ip>`, `cli:<user>` bzw. `chat`.
- Globaler Not-Aus und Circuit Breaker (Auto-Pause nach N Fehlschlägen) sind bewusst nicht Teil dieser Änderung.
