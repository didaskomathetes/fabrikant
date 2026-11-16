# ADR 0015: Freigaben bei belegtem Projekt-Lock einreihen

**Status:** Akzeptiert (2026-10-19, Merge #145; vorgeschlagen 2026-10-19; geändert durch ADR 0028)
**Kontext:** Runs eines Projekts teilen sich einen Worktree und werden per
Projekt-Lock serialisiert (`runs.project_lock`). Ein `needs_approval`-Run hält
den Lock während der Pause nicht. Wurde er freigegeben, während ein anderer Run
desselben Projekts lief, lehnte `_chat_resume` mit „läuft bereits“ ab. Die
Freigabe ging verloren, und der Mensch musste warten und dann erneut klicken.
Mit vielen Trigger-Runs (on_issue/on_pr-Reconcile, ADR 0014) ist das Projekt
fast immer belegt, die Freigabe war damit praktisch blockiert.

## Entscheidung

- Approve bei belegtem Lock wird **angenommen und eingereiht**: Der Run-Record
  bekommt `approval_queued: <ISO-Zeit>` und bleibt `needs_approval`. Ein
  Resume-Worker-Thread wartet **blockierend** auf den Projekt-Lock und setzt
  danach fort. `resumed` im API-Response ist `true`.
- Nach dem Lock-Erwerb prüft der Worker den Status erneut: Ein **Reject während
  des Wartens gewinnt** (kein Resume).
- **Idempotent:** pro Run höchstens ein Worker pro Prozess (`_resume_workers`).
  Ein erneutes Approve meldet „bereits freigegeben“.
- **Restart-fest:** `serve()` startet für alle `needs_approval`-Runs mit
  `approval_queued` wieder einen Worker (`requeue_approved_runs`).
- Die Serialisierung selbst bleibt (gemeinsamer Worktree, siehe
  framework-v1-draft).

## Konsequenzen

- Freigaben gehen nicht mehr verloren. Die UI zeigt den Wartezustand an.
- Ein wartender Worker blockiert einen Thread, bis der laufende Run endet.
  Hängt der Run, wartet die Freigabe mit (der Step-Timeout begrenzt das).
- Der CLI-Pfad (`fabrik approve`, eigener Prozess) bleibt synchron. Sein Lock
  ist prozesslokal und von diesem Problem nicht betroffen.
