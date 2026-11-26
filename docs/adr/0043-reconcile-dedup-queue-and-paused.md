# ADR 0043: Reconcile-Dedup — eingereihte Trigger und pausierte Runs

**Status:** Vorgeschlagen (2026-10-23)
**Entscheider:** Fabrikant (dogfooding, Issue #256 / #258)
**Ergänzt:** ADR 0028 (Dedup), ADR 0011 (Approval), ADR 0015 (Vorrang-Claim)
**Abgrenzung:** Issue #327 (Trigger-Runs überleben Restart) — bewusst nicht Teil
dieses ADRs, siehe „Abgelehnte Alternativen“.

## Kontext

Dogfooding-E2E von #256 (`Workers: 1`): zwei `approval`-Runs für `on_pr:#256`.
Ursachen: (1) Der Run-Record entsteht erst, wenn `work()` im Pool läuft; der
`reconcile`-Abgleich (`loop_run_triggers`) sieht wartende Aufträge nicht und reiht den
Trigger beim nächsten Poll erneut ein. (2) Ein pausierter `needs_approval`-Run gibt
seinen Claim frei (ADR 0028: „zählt nicht als aktiv“), ein zweiter Run desselben
Loops + Bezugs kann starten.

## Entscheidung

- `_safe_run` merkt sich eingereihte Trigger `(project, loop, trigger)` in-memory und
  reiht denselben Trigger nicht erneut ein. Der Eintrag fällt, sobald `work()` startet
  (ab dann schützen Claim bzw. Record); er ist prozesslokal wie Slots und Dedup.

  ```
  Pool (1 Worker):  [läuft: on_pr:#7]  [wartet: on_pr:#8]

  Poll-Tick feuert on_pr:#8 erneut:
    ohne Dedup:  [läuft: #7]  [wartet: #8]  [wartet: #8]   ← Doppel-Run
    mit Dedup:   [läuft: #7]  [wartet: #8]                 ← zweites Einreihen verworfen
  ```

- Ein nicht-blockierender `claim_run` liefert `None`, wenn ein Run im Status
  `needs_approval` für denselben Loop + `dedupe_key` (`issue_number`/`pr_number`)
  existiert. Loops ohne Bezug sind ausgenommen. Der Vorrang-Claim (`blocking=True`,
  manuelle Runs) wird nicht blockiert.

  ```
  r1 (approval, pr_number=5): needs_approval   ← Claim frei, Bezug belegt

  neuer Trigger on_pr:#5 → claim_run → None    (blockiert bis Approve/Reject)
  neuer Trigger on_pr:#6 → claim_run → OK      (anderer Bezug)
  ```

- Das **Resume eines pausierten Runs** blockiert sich nicht selbst: `claim_run`
  akzeptiert `exclude_run_id`, über das der fortgesetzte Run seinen eigenen
  `needs_approval`-Record aus der Prüfung nimmt (CLI-Approve claimt
  nicht-blockierend; der Chat-/Server-Resume läuft über `blocking=True`).

  ```
  CLI: fabrikant approve r1        (r1 = needs_approval, pr_number=5)

  claim_run(vars={pr_number: 5})                          → None  ✖ (eigener Record)
  claim_run(vars={pr_number: 5}, exclude_run_id="r1")     → Claim ✔
  ```

## Begründung

- **In-Memory-Dedup für wartende Aufträge:** Der Zustand „eingereiht, noch kein
  Record“ existiert nur im Prozess; ein in-memory-Set hat exakt dieselbe
  Lebensdauer wie die Pool-Queue, die es dedupliziert. Persistenz wäre nur
  nötig, wenn der Auftrag Restarts überleben müsste — das ist Issue #327.
- **Pausierte Runs belegen den Bezug weiter:** Ein `needs_approval`-Run hat seine
  Arbeit (z. B. den PR-Branch) bereits vorbereitet; ein zweiter Run desselben
  Bezugs würde dieselbe Arbeit doppelt leisten und zwei Freigabe-Anfragen
  erzeugen. Blockieren bis Approve/Reject ist das erwartete Verhalten, kein
  Edge Case.
- **`exclude_run_id` statt Statuswechsel vor dem Claim:** Den Record vor dem
  Claim auf `running` zu setzen, würde den Zustandsautomaten umordnen (Claim
  kann `None` liefern → Record müsste zurückgesetzt werden). Die Ausnahmeregel
  bleibt lokal und berührt keine anderen Pfade.

## Abgelehnte Alternativen

- **Wartende Trigger als `queued`-Records persistieren:** Löst zusätzlich den
  Restart-Verlust (Issue #327) an, braucht aber den zentralen Start-Use-Case und
  die Start-Policy aus #327 — zu groß für diesen Fix. Bis #327 bleibt der
  Eintrag prozesslokal; nach einem Restart ist der wartende Auftrag verloren
  (wie heute), der nächste Poll/Cron-Tick reiht ihn neu ein.
- **Resume per Statuswechsel (`needs_approval` → `running`) vor dem Claim:**
  Siehe Begründung; würde den Claim-Ausfall-Pfad komplizierter machen.
- **`needs_approval`-Runs zählen als aktiv (Claim nicht freigeben):** Würde
  Slots (`max_parallel`) belegen, die der pausierte Run nicht nutzt, und
  ADR 0028 („zählt nicht als aktiv“) aufheben.

## Folgen

- Keine doppelten Freigabe-Anfragen / doppelte Arbeit bei `refine`/`develop`.
- Ein pausierter Run blockiert neue Trigger für denselben Bezug bis Approve/Reject;
  `claim_run` liest dafür die Run-Records des Projekts (Datei-I/O pro Claim —
  vertretbar, bis der SQL-Store von ADR 0034 die Records ersetzt).
- Event-Trigger (`on_pr` ohne `reconcile`) entfallen, solange derselbe Trigger noch
  wartet; der wartende Auftrag trägt die älteren Variablen.
- Regression-Tests in `tests/test_run_slots.py` (Claim-Dedup) und
  `tests/test_cli.py` (Resume mit `vars`).
