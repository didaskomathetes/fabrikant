# Relay-Tasks — Anleitung

Entscheidung und Begründung: [ADR 0037](adr/0037-relay-tasks.md). Hier: Brief schreiben, Ablauf,
Eingreifen.

## Brief-Vorlage (Issue-Body)

Titel = Aufgabe. Pflicht sind `## Goal`, `## Session` und `## Verify`; der Rest ist optional.
Sprache der Sessions ist Englisch, der Brief darf deutsch sein.

````markdown
## Goal
What is true when the task is finished (one paragraph). What is explicitly out of scope.

## Session
How a single session works:
- Sources / targets and how to reach them (profile names, endpoints, never secrets)
- Unit of work (e.g. batches of 200 rows, keyed by `source_id`; idempotent upsert)
- Required backup before the first write (e.g. an export or snapshot of the target)
- Checkpoints: "stop as blocked after the first batch for review"

## Verify
Invariants fixed before work starts (the auditor checks exactly these):
- target item count == source pairs after filter X
- no duplicate `source_id` in the target
- content hash per item over (question, answer, source_id) matches the source
- required metadata present: shop, date, channel
- 20 random items compared side by side

```verify
uv run python -m my_import --verify --dry-run
```

## Audit access
Read-only access for the auditor (profile / role / API key name). Without it the audit stops.

## Settings
max_sessions: 30
session_minutes: 30
scope: src/my_import/**, docs/my_import.md
````

- **`## Verify` is pinned.** The first session stores its hash. A later change blocks the task;
  removing `relay: blocked` adopts the new version.
- **```` ```verify ````** (optional): a command fabrikant runs after every session. Its last output
  line is the progress evidence (stall detection, status comment). Without it, the `progress`
  field in the handoff counts.
- **Lint:** fabrikant writes `.fabrikant/relay/<N>/ruff.toml` (`exclude = ["*"]`) so worker scripts and audit
  evidence stay byte-for-byte and don't fail the target repo's ruff check. An existing file is kept.
- **`scope`**: paths a session may commit in addition to `.fabrikant/relay/<N>/`. Everything else
  blocks the task and is **reverted** at check-in (tracked → HEAD, untracked → deleted), so
  unblocking does not re-block on the same paths; the spec's `relay.forbid` always wins.
  The check compares against the commit at checkout: commits the agent makes itself are folded
  back into the worktree (fabrikant commits). An uncommitted merge of `origin/<base>` is allowed:
  paths that come unchanged from the base don't count, an out-of-scope edit falls back to the
  base's version, and fabrikant commits the complete merge. A merge of anything else is dropped.
- **`session_minutes`** is capped by `relay.max_session_minutes` (default 35, because of
  `FABRIKANT_STALE_HOURS`).

## Ablauf

1. Issue mit Brief anlegen, Label `fabrikant-relay` setzen. Das Label muss von einem `relay.allowed`-
   Login kommen.
2. `relay-dispatch` (Default alle 15 min) startet die erste Session, danach jeweils den fälligen
   Task, der am längsten wartet. Es läuft höchstens ein Relay-Run pro Projekt.
3. Jede Session committet und pusht auf `fabrikant/relay/<N>` und aktualisiert den Status-Kommentar.
   Commits laufen als Bot (`relay.bot_login`). Bei `done` steht das Ergebnis (`## Result` im
   Handoff) im Status-Kommentar, nach dem Audit zusätzlich dessen Zusammenfassung.
4. `status: done` → Label `relay: done` → `relay-audit`:
   - Phase 1 blind: eigene Queries, read-only. Veröffentlicht werden nur Dateien direkt in
     `audit/blind/` (max. 1 MB); Arbeitskopien gehören nach `.fabrikant-session/audit/work/`
   - Phase 2: prüft die Skripte und Logs des Workers
   - `AUDIT.md` mit Verdikt; `pass` → Label `relay: audited`. Das Verdikt ist an den Trail-Hash
     gebunden (letzte Zeile von `AUDIT.md`, Status-Kommentar)
   - mit `relay.trail_archive`: nach `pass` Trail archivieren (`audit-<k>`), Archiv prüfen, dann
     `.fabrikant/relay/<N>/` per Lösch-Commit vom Branch entfernen; der PR enthält nur noch die
     Änderung. Link und Hash stehen im Status-Kommentar und in einem Block am Ende des PR-Bodys (Archiv-Link, Ref,
     `AUDIT.md`, Hash, Prüfbefehl; fabrikant ersetzt nur den Text zwischen den `fabrikant-relay-trail`-Markern
     und entfernt ihn, wenn eine spätere Session den Trail zurückholt)
5. Ein Mensch zeichnet ab: Stichprobe aus dem Audit im Zielsystem prüfen, bei Code einen PR vom
   Branch öffnen, Issue schließen.

## Eingreifen

| Situation | Was tun |
|---|---|
| Checkpoint / Rückfrage (`relay: blocked`) | Status-Kommentar lesen, ggf. Brief ergänzen, Label entfernen |
| Stillstand, gescheiterte Runs (ohne `retry` bzw. Klasse nicht in `retry.on`, `retry.max` erschöpft), `max_sessions` | Ursache klären (Run-Historie: Suche `relay #N`), ggf. `max_sessions` erhöhen, Label entfernen |
| Brief von jemand anderem bearbeitet | prüfen, dann Label `fabrikant-relay` neu setzen (= Freigabe dieser Fassung) |
| Audit `fail` | `AUDIT.md` lesen; Label entfernen → nächste Session arbeitet die Findings ab |
| Weiterarbeiten nach archiviertem Trail | Labels `relay: done`/`relay: audited` entfernen; die nächste Session stellt den Trail aus dem Archiv wieder her (Hash-Prüfung) |
| Selbst weitermachen | Branch auschecken, nach `prompts/relay/session.prompt.md` arbeiten, Handoff aktualisieren, pushen |
| Anhalten | `fabrikant pause <projekt> relay-dispatch` oder Label `fabrikant-relay` entfernen |

## Gescheiterte Runs

Ein Run, dessen `publish` nicht durchlief, ist ein Fehlschlag. Ohne `relay.retry` blockiert er den
Task sofort. Mit `relay.retry` (gleiche Semantik wie der Run-Retry, ADR 0036) wird nur bei
`interrupted`/`transient` (Fehlerklassen, ADR 0035) nach dem Backoff wiederholt, höchstens `max`
zusätzliche Male; meldet der Run ein `retry_after` (z. B. Rate-Limit-Reset), wartet der Retry bis
dahin, höchstens `max_wait`. Die Relay-Steps melden ihre Klasse über den Exit-Code (75 = `transient`:
Netz, GitHub-5xx, Rate-Limit; 78 = `environment`: 401/403, falsches `bot_login`). `unknown` blockiert
immer.

## Grenzen

- Ohne `FABRIKANT_SANDBOX=1` (ADR 0054) laufen Agents als OS-User. Die Prüfungen der Harness verhindern
  Drift, nicht Vorsatz. Darum führt fabrikant nur Briefs von `allowed`-Logins aus.
- Lokale Dateien pro Run: `.fabrikant-session/` ist das Austauschverzeichnis mit dem Agenten (Brief,
  `SESSION.md`, Audit-Arbeitsstand); die Sandbox trägt es in den Container und zurück, ohne
  `audit/work/` (steht in `.fabrikant-session/.fabrikant-local`). Plan, Handoff-Snapshot und Check-in liegen
  in `.fabrikant-harness/` und erreichen den Agenten in der Sandbox nie.
- Read-only Zugang für den Prüfer stellt das Projekt bereit; fabrikant kann ihn nicht erzwingen.
- Der Prüfer kann nur die Invarianten prüfen. Nicht spezifizierte Transformationen meldet er,
  bewerten muss sie ein Mensch.

## Configuration (ADR 0037)

For tasks that don't fit into a single run (historical import, migration, analysis over days). An issue with the
label `fabrikant-relay` is the assignment. It runs in many short sessions, the handoff file lives on the branch
`fabrikant/relay/<N>`. On `done`, an independent, initially blind audit checks the result. Enable per project:

```yaml
relay:
  allowed: [alice]             # required: only these logins may approve briefs (label + body)
  forbid: [infrastructure/**]  # paths that sessions never commit
  builder: {model: ...}        # sessions (github_identity dev)
  reviewer: {model: ...}       # audit (github_identity reviewer, read-only)
  bot_login: my-builder-app    # required: read the status comment only from this identity; commit author
  retry: {max: 3, backoff: [5m, 30m, 2h], "on": [interrupted, transient], max_wait: 24h}  # like run retry (ADR 0036); off by default
  trail_archive: {type: git-ref}  # after audit pass, archive the trail + remove it from the branch (git-ref | local)
  # optional: repo, base, label, dispatch (cron), max_session_minutes, verify_timeout,
  #           audit_timeout, audit: false
```

From this fabrikant generates the loops `relay-dispatch` (cron, no agent), `relay-session` and `relay-audit`.
A human unblocks (checkpoint, stall, changed `## Verify`) by removing the label `relay: blocked`. The dispatcher
needs `FABRIKANT_URL` (default `http://127.0.0.1:8321`) and, if applicable, `FABRIKANT_TOKEN` for
`POST /api/runs`.

**Trail archive** (ADR 0037, addendum 2026-11-03): with `trail_archive`, `audit-publish` stores the trail
(`.fabrikant/relay/<N>/`) immutably after `pass` (`git-ref`: hidden ref `refs/fabrikant/relay/<N>/audit-<k>`;
`local`: `FABRIKANT_DATA/relay-archive/`), checks the archive against the trail hash and only then removes the
trail from the branch with a delete commit. The PR diff and merge thus contain only the actual change. The
verdict is bound to the hash (last line of `AUDIT.md`, status comment). If a later session or a repeated audit
needs the trail, it is restored from the archive (hash check, otherwise the step fails). Without `trail_archive`
the trail stays on the branch; the status comment warns as soon as a PR from the branch exists.
