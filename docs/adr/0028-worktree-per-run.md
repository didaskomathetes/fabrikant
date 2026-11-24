# ADR 0028: Worktree pro Run statt Projekt-Lock

**Status:** Akzeptiert (2026-10-20, Merge #238; vorgeschlagen 2026-10-20; geändert 2026-10-23: Worktree-Lease statt Single-Process-Garantie, #237/PR #328)
**Ersetzt:** den Projekt-Lock (`runs.project_lock`, Referenz im Issue: ADR 0001 §4).
**Ändert:** ADR 0015 (Warten auf den Projekt-Lock wird zum Warten auf einen Run-Slot) und
ADR 0020 (`worktree: false`-Loops belegen keinen Slot statt eines eigenen Locks).
**Hinweis:** Im Issue #181/#190 als „ADR 0019“ geführt; die Nummer war auf main bereits für
Pause/Resume/Disable vergeben (beim Rebase auf 0023 verschoben, bei der
Nummern-Bereinigung 2026-10-20 auf 0028).

## Kontext

Alle Loops eines Projekts teilten sich einen Worktree und liefen über einen
Projekt-Lock seriell. Ein `develop`-Run (Timeout 3600 s) blockierte so bis zu
eine Stunde lang `review-pr`, `pr-reply`, `refine` und `sonar-scan`; Cron- und
Poll-Runs ohne Lock wurden verworfen.

## Entscheidung

- **Worktree pro Run:** `<WORKTREES_DIR>/<projekt>/<run_id>`, detached auf
  `origin/<branch>`, angelegt beim Run-Start. Ein Resume nach Approval nutzt den
  vorhandenen Worktree unverändert (kein fetch/reset). Specs ohne `repo:`
  arbeiten weiter direkt im `root` (keine Isolation, kein Cleanup), siehe „Specs ohne `repo:`“.
- **Cleanup:** Am Run-Ende (pass/fail/error) und beim Reject wird der Worktree
  entfernt; ein Run in `needs_approval` behält ihn. Beim Server-Start entfernt
  `cleanup_orphaned_worktrees` Worktrees verwaister Runs (Crash/Kill) und
  Legacy-Worktrees (`<projekt>/` als gemeinsamer Worktree). Worktrees pausierter
  Runs bleiben.
- **Run-Slots statt Lock:** `max_parallel` (Projekt-Spec, int ≥ 1, Default
  `config.DEFAULT_MAX_PARALLEL` = 3) begrenzt gleichzeitige Runs pro Projekt.
  Trigger-Runs (Cron/Poll) über dem Limit werden **verworfen** (der nächste Tick
  versucht es erneut). Manuelle/Chat-Runs und freigegebene Resumes **warten** auf
  einen freien Slot und haben Vorrang vor neuen Trigger-Runs (ADR 0015).
  `worktree: false`-Loops (ADR 0020) belegen keinen Slot (kein Worktree).
- **Dedup:** Pro Loop + Issue/PR (`issue_number`/`pr_number`) höchstens ein aktiver
  Run; Loops ohne Bezug (Cron) höchstens einen pro Loop. Die Prüfung ist
  prozesslokal (in-memory, atomar mit dem Slot-Erwerb); ein pausierter
  `needs_approval`-Run zählt nicht als aktiv.
- **Cleanup-Fehler:** Agent-State-Merge und Worktree-Cleanup werden getrennt
  behandelt. Fehler (`OSError`, git-Subprozess) und ein nach dem Entfernen noch
  vorhandener Worktree landen als `state_merge_error` / `worktree_cleanup_error` im
  Run-Record und im Run-Log; der Run-Status bleibt unverändert. Schlägt der Merge
  fehl, bleibt der Worktree (enthält den ungemergten Stand) bis zum nächsten
  Server-Start stehen.
- **Gemeinsamer Agent-State:** `state/replied-comments.json` und
  `state/reviewed-prs.json` liegen kanonisch unter `STATE_DIR/agent/<projekt>/`.
  Der Run bekommt beim Start (und Resume) eine Kopie im Worktree (`<workdir>/state/`,
  Agent-Prompts bleiben unverändert). Am Run-Ende bzw. bei der Pause werden nur
  die **Änderungen des Runs** (neue/geänderte/gelöschte Keys gegenüber dem
  Snapshot) unter Datei-Lock (Thread-Lock + `flock`) in den gemeinsamen Stand
  gemerged — parallele Updates gehen nicht verloren.
  Der `state`-Step sperrt sein Read-Modify-Write mit demselben Lock.

## Konsequenzen

- Lange Runs blockieren kurze nicht mehr. Der Worker-Pool (`FABRIKANT_WORKERS`) begrenzt
  die Parallelität zusätzlich — für echte Parallelität muss er > 1 sein.
- Mehr Plattenplatz und `git fetch`-Last (ein Worktree pro Run); gleichzeitige Runs
  desselben Projekts dürfen nicht denselben Branch auschecken (Branch-Namen pro
  Issue/Run, vgl. #25; Dedup deckt den Regelfall ab).
- Der Slot-Zähler ist prozesslokal: `fabrikant approve` (CLI) zählt nicht gegen einen
  laufenden Server. Der Agent-State ist dagegen prozessübergreifend gelockt.
- Andere Dateien in `state/` (z. B. `reply-<n>.md`, `review-<n>.json`) sind
  run-lokal und gehen mit dem Worktree verloren; nur die zwei oben genannten werden
  geteilt. Weitere gemeinsame Dateien: `agent_state.SHARED_FILES` erweitern.

## Worktree-Lease und Single-Process-Garantie

Jeder Run-Worktree hat eine Lease-Datei `<WORKTREES_DIR>/<projekt>/<run_id>.lease`
(JSON: `pid`, `heartbeat`, `token`; außerhalb des Git-Trees). Sie wird vor dem Anlegen des
Worktrees geschrieben, per Heartbeat-Thread alle `config.WORKTREE_LEASE_HEARTBEAT_S` (30 s)
erneuert und erst nach Agent-State-Merge und Worktree-Cleanup (Run-Ende, Pause) bzw. bei der
Worktree-Entfernung gelöscht. Der Heartbeat erneuert nur die eigene Lease (Token); ist sie
weg oder übernommen, legt er sie nicht neu an. Schreiben/Erneuern/Löschen der Lease laufen
unter einem projektweiten `flock` (`gitops.lease_lock`, `<WORKTREES_DIR>/<projekt>.lock`);
der Lock ist hostlokal, mehrere Instanzen/Hosts regelt #280. Im Cleanup läuft nur die
Prüfung + der Claim („Lease prüfen → Lease entfernen“) unter `lease_lock`; das eigentliche
Entfernen des Worktrees (`remove_worktree`, unter `repo_lock`) erfolgt **außerhalb** von
`lease_lock`, damit `lease_lock` nie über die `repo_lock`-Erwerbung gehalten wird
(`repo_lock` kann bis zu ~600 s blockieren: `git fetch` + `git worktree add` eines
parallelen Runs). Der Claim ist sicher, weil Worktree-Pfade nach eindeutigem `run_id`
geschlüsselt sind: ein neuer Run schreibt nie eine Lease auf einen gecleanten Pfad, und der
Heartbeat des verwaisten Runs ist tot.
`cleanup_orphaned_worktrees` überspringt einen Worktree, dessen Lease von einem
**anderen** Prozess stammt, dessen PID lebt und dessen Heartbeat jünger als
`config.WORKTREE_LEASE_TIMEOUT_S` (300 s) ist. Fehlende/korrupte Lease, eigene PID, tote
PID oder abgelaufener Heartbeat schützen nicht; `needs_approval` schützt weiterhin.
Das Timeout muss nur das Heartbeat-Intervall plus einen Puffer für ein einmalig
blockiertes Heartbeat-Schreiben übersteigen (`300 s > 30 s + Puffer`), **nicht** den
`repo_lock`-Halt: da `lease_lock` nur für die kurze Prüfung/den Claim gehalten wird,
können Heartbeats lebender Runs nie so lange blockiert werden, dass eine lebende Lease
abläuft.

Damit löscht ein zweiter Prozess (Server, `fabrikant run`/`approve`) keine Worktrees
laufender Runs mehr. **Weiterhin offen** und nicht unterstützt: Slots (`max_parallel`),
Dedup und Vorrang-Warter (`runs.claim_run`) sind prozesslokal; parallele Prozesse auf
demselben Projekt können das Slot-Limit überschreiten und denselben Trigger doppelt
starten. Prozessübergreifende Slots/Dedup sind ein Folge-Thema (#280). Einschränkung: PID-Prüfung
gilt nur auf demselben Host.

## Specs ohne `repo:`

Ohne `repo:` gibt es keinen Worktree; Runs arbeiten direkt im `root`. Parallele Runs
würden sich dort überlappen (Git-Index, Dateien). Daher gilt für diese Specs fest
`max_parallel: 1` (`runs.max_parallel_for`, ein gesetzter höherer Wert wird ignoriert).
`worktree: false`-Loops sind davon ausgenommen (eigene Scratch-Workdir).

**Known Gap:** Das Problem besteht nur bei geteiltem Dateisystem (Single-Machine/
Homelab-Betrieb). Bei container-basierten Runs (Sandbox pro Run mit eigenem Checkout,
vgl. #20) löst es sich auf; der Sync-back von Änderungen für repo-less Specs bleibt dort
offen.
