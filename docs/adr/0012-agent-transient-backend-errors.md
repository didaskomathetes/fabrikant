# ADR 0012: Agent-Step resilient gegen transiente Backend-Fehler

**Status:** Akzeptiert (2026-10-18)
**Entscheider:** Fabrik (dogfooding, Issue `pr-reply FAILED 20261018T141501Z`)
**Kontext:** Der `pr-reply`-Loop (cron `*/15`, agent-Step über `opencode serve`)
lief am 2026-10-18 in einen 6h-Hang und wurde erst vom Dead-Man's-Switch als
`error` (`stale: >6h in 'running'`) markiert. Der agent-Step hat ein Step-Timeout
(1800s), das eine hängende *Session* fängt — aber **nicht** einen
Backend-/Transport-Fehler: warf `be.poll()`/`be.transcript()` eine Exception
(z. B. `opencode serve` down/Restart mitten im Run), propagierte sie aus dem
Polling-Loop und killte den gesamten Run. Für einen cron-Agent-Loop, der alle
15 min feuert, ist ein transienter Backend-Ausfall kein Run-Fehler — der nächste
Run 15 min später würde ohnehin neu starten.

## Entscheidung

Der agent-Step (`src/fabrik/steps/agent.py`, `_step_agent_backend`) behandelt
**Transport-Fehler während des Pollings** als transient:

- `be.poll()`/`be.transcript()` werfen → der Step killt den Run **nicht**,
  sondern loggt den Fehler, wartet mit Backoff (`5s, 10s, …`, Cap 30s) und
  pollt weiter — **innerhalb des Step-Timeouts**.
- Erholt sich das Backend vor dem Timeout → der Step läuft normal zu Ende
  (`done`).
- Ist das Timeout erreicht, während das Backend weiter fehlerhaft ist → der
  Step meldet `agent timeout nach <X>s … · Backend-Fehler: <ursache>` (die
  Ursache ist damit im Run-Log/Record, statt nur "timeout nach 1800s").
- Die Session wird **nicht** vorzeitig gecancelt (Cancel bleibt beim Timeout /
  bei `failed`).

**Unverändert:** Status-basierte Fehler (`poll()` → `failed` + `retryable`)
starten die Session neu (`step.retries`, Default 2 → max. 3 Versuche). Das ist
ein anderer Pfad (Session-Neustart) und bleibt so.

## Begründung
- **Cron-Loops sind idempotent:** `pr-reply`/`review-pr` laufen periodisch und
  sind state-basiert dedupliziert (`state/replied-comments.json`,
  `state/reviewed-prs.json`). Ein transienter Backend-Ausfall kostet keinen
  Fortschritt — der nächste Run holt es ein. Den Run zu killen ist reine
  Reibung (Alert + Issue + manueller Nachlauf).
- **Symmetrie zum bestehenden Retry:** Der Step retryt bereits transiente
  *Status*-Fehler. Transport-Fehler sind derselbe Fehlerklassen-Typ (Backend
  kurz nicht erreichbar) — es fehlt nur die Exception-Variante.
- **Bounded:** Das Verhalten ist durch das Step-Timeout begrenzt (kein
  endloser Retry-Loop); im Worst Case endet der Step mit demselben Timeout wie
  zuvor, nur mit besserer Diagnose.

## Abgelehnte Alternativen
- **Exception sofort als Step-Fehler weiterleiten (Status quo):** Killt cron-Runs
  bei jedem Backend-Blip → Alert-Flut, genau das beobachtete Problem.
- **Session bei Transport-Fehler neu starten (wie `failed`+`retryable`):**
  Würde den Prompt neu senden und bereits erledigte Arbeit (Commits, Replies)
  doppelt ausführen. Bei einem kurzen Ausfall ist Weiterpollen (Session bleibt
  server-seitig) der sicherere Pfad.
- **Dead-Man's-Switch-Threshold senken:** Fängt nur *Prozess*-Tod, nicht
  Backend-Blips; und ein global kürzeres Fenster würde lange `develop`-Runs
  (3600s) falsch als stale markieren.

## Folgen
- agent-Steps (alle Backends) sind robust gegen transiente
  Backend-/Transport-Fehler innerhalb des Step-Timeouts.
- Regression-Tests in `tests/test_steps_agent.py`
  (`test_agent_transient_transport_error_then_success`,
  `test_agent_persistent_transport_error_times_out`,
  `test_agent_transport_error_does_not_cancel_prematurely`).
- Der Dead-Man's-Switch bleibt der Backstop für *Prozess*-Tod (unverändert).
