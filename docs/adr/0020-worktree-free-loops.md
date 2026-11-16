# ADR 0020: Worktree-freie Loops

**Status:** Akzeptiert (2026-10-20, Merge #231; vorgeschlagen 2026-10-20; geändert durch ADR 0028)

## Kontext

Loops eines Projekts teilen sich normalerweise den Git-Worktree. Dadurch
werden sie mit dem Projekt-Lock serialisiert, auch wenn ein Loop nur
spec-validierende oder sonstige Schritte ausführt, die keinen Worktree
benötigen. Für solche Loops soll parallele Ausführung möglich sein, ohne
Git-Änderungen oder einen gemeinsamen Arbeitsstand zu riskieren.

## Entscheidung

- Ein Loop nutzt standardmäßig den Projekt-Worktree und den Projekt-Lock.
- Mit `worktree: false` erhält jeder Run eine eigene, leere Scratch-Workdir
  unter `state/scratch/<run_id>` und einen eigenen, stabilen Lock pro Loop.
  Solche Loops laufen parallel zu anderen Loops des Projekts, aber nicht
  doppelt.
- `worktree: false` ist nur zulässig, wenn der Loop weder direkt noch in
  verschachtelten `verify.checks` einen `git`-, `pr`-, `agent`-, `review`-
  oder `pr_review`-Step enthält.
- Die Lock-Wahl wird vor dem Run gegen den geladenen Loop-Snapshot
  abgeglichen. Ändert sich die Spec zwischen Lock-Wahl und Run, fällt ein
  Wechsel von einem Loop-Lock zu einem Worktree-Run auf den Projekt-Lock
  zurück.
- Die Scratch-Workdir wird nach jedem Run entfernt. Fehler beim Aufräumen
  werden im Run-Log festgehalten und beenden den Run nicht.

## Konsequenzen / Nicht-Ziele

- Worktree-freie Loops dürfen keine Git- oder PR-Arbeit ausführen; ihre
  Scratch-Workdir ist kein Ersatz für einen persistenten Checkout.
- Die bestehende Prioritätsbehandlung für Freigaben und manuelle Runs gilt
  pro Lock-Schlüssel und bleibt zwischen unabhängigen Loop-Locks getrennt.
- Die Scratch-Workdir wird beim Start geleert, damit ein wiederverwendeter
  `run_id` keinen alten Inhalt übernimmt.
