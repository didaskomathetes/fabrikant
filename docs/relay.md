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
- **```` ```verify ````** (optional): a command fabrik runs after every session. Its last output
  line is the progress evidence (stall detection, status comment). Without it, the `progress`
  field in the handoff counts.
- **Lint:** fabrik writes `.fabrik/relay/<N>/ruff.toml` (`exclude = ["*"]`) so worker scripts and audit
  evidence stay byte-for-byte and don't fail the target repo's ruff check. An existing file is kept.
- **`scope`**: paths a session may commit in addition to `.fabrik/relay/<N>/`. Everything else
  blocks the task and is **reverted** at check-in (tracked → HEAD, untracked → deleted), so
  unblocking does not re-block on the same paths; the spec's `relay.forbid` always wins.
  The check compares against the commit at checkout: commits the agent makes itself are folded
  back into the worktree (fabrik commits). An uncommitted merge of `origin/<base>` is allowed:
  paths that come unchanged from the base don't count, an out-of-scope edit falls back to the
  base's version, and fabrik commits the complete merge. A merge of anything else is dropped.
- **`session_minutes`** is capped by `relay.max_session_minutes` (default 35, because of
  `FABRIK_STALE_HOURS`).

## Ablauf

1. Issue mit Brief anlegen, Label `fabrik-relay` setzen. Das Label muss von einem `relay.allowed`-
   Login kommen.
2. `relay-dispatch` (Default alle 15 min) startet die erste Session, danach jeweils den fälligen
   Task, der am längsten wartet. Es läuft höchstens ein Relay-Run pro Projekt.
3. Jede Session committet und pusht auf `fabrik/relay/<N>` und aktualisiert den Status-Kommentar.
   Commits laufen als Bot (`relay.bot_login`). Bei `done` steht das Ergebnis (`## Result` im
   Handoff) im Status-Kommentar, nach dem Audit zusätzlich dessen Zusammenfassung.
4. `status: done` → Label `relay: done` → `relay-audit`:
   - Phase 1 blind: eigene Queries, read-only. Veröffentlicht werden nur Dateien direkt in
     `audit/blind/` (max. 1 MB); Arbeitskopien gehören nach `.fabrik-session/audit/work/`
   - Phase 2: prüft die Skripte und Logs des Workers
   - `AUDIT.md` mit Verdikt; `pass` → Label `relay: audited`. Das Verdikt ist an den Trail-Hash
     gebunden (letzte Zeile von `AUDIT.md`, Status-Kommentar)
   - mit `relay.trail_archive`: nach `pass` Trail archivieren (`audit-<k>`), Archiv prüfen, dann
     `.fabrik/relay/<N>/` per Lösch-Commit vom Branch entfernen; der PR enthält nur noch die
     Änderung. Link und Hash stehen im Status-Kommentar und in einem Block am Ende des PR-Bodys (Archiv-Link, Ref,
     `AUDIT.md`, Hash, Prüfbefehl; fabrik ersetzt nur den Text zwischen den `fabrik-relay-trail`-Markern
     und entfernt ihn, wenn eine spätere Session den Trail zurückholt)
5. Ein Mensch zeichnet ab: Stichprobe aus dem Audit im Zielsystem prüfen, bei Code einen PR vom
   Branch öffnen, Issue schließen.

## Eingreifen

| Situation | Was tun |
|---|---|
| Checkpoint / Rückfrage (`relay: blocked`) | Status-Kommentar lesen, ggf. Brief ergänzen, Label entfernen |
| Stillstand, gescheiterte Runs (ohne `retry` bzw. Klasse nicht in `retry.on`, `retry.max` erschöpft), `max_sessions` | Ursache klären (Run-Historie: Suche `relay #N`), ggf. `max_sessions` erhöhen, Label entfernen |
| Brief von jemand anderem bearbeitet | prüfen, dann Label `fabrik-relay` neu setzen (= Freigabe dieser Fassung) |
| Audit `fail` | `AUDIT.md` lesen; Label entfernen → nächste Session arbeitet die Findings ab |
| Weiterarbeiten nach archiviertem Trail | Labels `relay: done`/`relay: audited` entfernen; die nächste Session stellt den Trail aus dem Archiv wieder her (Hash-Prüfung) |
| Selbst weitermachen | Branch auschecken, nach `prompts/relay/session.prompt.md` arbeiten, Handoff aktualisieren, pushen |
| Anhalten | `fabrik pause <projekt> relay-dispatch` oder Label `fabrik-relay` entfernen |

## Gescheiterte Runs

Ein Run, dessen `publish` nicht durchlief, ist ein Fehlschlag. Ohne `relay.retry` blockiert er den
Task sofort. Mit `relay.retry` (gleiche Semantik wie der Run-Retry, ADR 0036) wird nur bei
`interrupted`/`transient` (Fehlerklassen, ADR 0035) nach dem Backoff wiederholt, höchstens `max`
zusätzliche Male; meldet der Run ein `retry_after` (z. B. Rate-Limit-Reset), wartet der Retry bis
dahin, höchstens `max_wait`. Die Relay-Steps melden ihre Klasse über den Exit-Code (75 = `transient`:
Netz, GitHub-5xx, Rate-Limit; 78 = `environment`: 401/403, falsches `bot_login`). `unknown` blockiert
immer.

## Grenzen

- Ohne `FABRIK_SANDBOX=1` (ADR 0054) laufen Agents als OS-User. Die Prüfungen der Harness verhindern
  Drift, nicht Vorsatz. Darum führt fabrik nur Briefs von `allowed`-Logins aus.
- Lokale Dateien pro Run: `.fabrik-session/` ist das Austauschverzeichnis mit dem Agenten (Brief,
  `SESSION.md`, Audit-Arbeitsstand); die Sandbox trägt es in den Container und zurück, ohne
  `audit/work/` (steht in `.fabrik-session/.fabrik-local`). Plan, Handoff-Snapshot und Check-in liegen
  in `.fabrik-harness/` und erreichen den Agenten in der Sandbox nie.
- Read-only Zugang für den Prüfer stellt das Projekt bereit; fabrik kann ihn nicht erzwingen.
- Der Prüfer kann nur die Invarianten prüfen. Nicht spezifizierte Transformationen meldet er,
  bewerten muss sie ein Mensch.
