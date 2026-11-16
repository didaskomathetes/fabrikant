# ADR 0037: Relay-Tasks — langlaufende Aufgaben als Kette kurzer Agent-Sessions

**Status:** Draft (2026-10-21; überarbeitet 2026-10-27: Verweise auf ADR 0035/0036 umgestellt,
`retry` um `max_wait`/`retry_after` ergänzt, Exit-Code-Klassen der Relay-Steps, Label-Pflicht bei der
Autorisierung, `bot_login`-Prüfung, Audit schreibt nur den Report; 2026-11-03 Nachtrag: Archiv des
Audit-Trails, Trail aus dem PR entfernen)
**Umsetzung:** `src/fabrik/relay/`, `prompts/relay/`
**Baut auf:** ADR 0008 (untrusted Input in Shell-Steps), ADR 0010 (Dual-Identity), ADR 0020
(worktree-freie Loops), ADR 0028 (Worktree pro Run).
**Verwendet:** ADR 0035 (Fehlerklassifikation) und ADR 0036 (Run-Retry). Relay übernimmt deren
Fehlerklassen und die `retry`-Semantik (`max`, `backoff`, `on`, `max_wait`); nur der Zustand der
Wiederholung liegt am Task statt in einer eigenen Kette.

## Kontext

Manche Aufgaben passen nicht in einen Run: ein historischer Datenimport, eine Migration, eine
Analyse über Stunden oder Tage. Ein Run dauert höchstens `FABRIK_STALE_HOURS` (lokal 1 h). Ein
Server-Neustart bricht laufende Runs ab, und ein langer Run belegt den einzigen Worker. Solche
Aufgaben sind außerdem selten Code mit PR: oft entstehen nur Ad-hoc-Skripte, SQL und Daten in einem
Zielsystem. Ein klassisches Code-Review greift dann nicht.

Gebraucht wird ein **projektneutrales Werkzeug**: Eine Aufgabe wird beschrieben und läuft in vielen
kurzen Sessions, die zu verschiedenen Zeiten starten. Der Fortschritt ist für das Team sichtbar, und
der Erfolg wird unabhängig geprüft.

## Entscheidung

1. **Issue = Auftrag, Branch = Zustand.** Ein Issue mit Opt-in-Label (Default `fabrik-relay`) ist
   der *Brief*. Pflichtabschnitte sind `## Goal`, `## Session` (wie eine Session arbeitet) und
   `## Verify` (Invarianten, optional mit ```` ```verify ````-Kommando). Optional:
   `## Audit access` (read-only Zugang für den Prüfer) und `## Settings` (`max_sessions`,
   `session_minutes`, `scope`). Der Zustand liegt auf dem Branch `fabrik/relay/<N>` in
   `.fabrik/relay/<N>/`:
   - `HANDOFF.md`: YAML-Front-Matter (`status: active|blocked|done`, `session`, `progress`,
     `not_before`, …) plus Markdown (`## State`, `## Next`, `## Audit facts`, `## Log`)
   - `scripts/`: jedes ausgeführte Skript und SQL
   - `sessions/s<k>.md`: was Session k ausgeführt hat
   - `AUDIT.md`, `audit/`: Audit-Report und Blind-Queries

   Pushes gehen nur auf diesen Branch, nie auf geschützte Branches.

2. **Feature statt Projekt-Loops.** Ein Projekt schaltet Relay mit einem `relay:`-Block frei.
   `specs.load_spec` erzeugt daraus die Loops `relay-dispatch`, `relay-session` und `relay-audit`
   (Namen reserviert); die Prompts sind generisch. Projektwissen kommt aus Brief und Repo,
   Projekt-Konfiguration aus dem Block:

   ```yaml
   relay:
     allowed: [alice]              # Pflicht: wer Briefs freigeben darf
     forbid: [infrastructure/**]   # nie committen
     builder: {model: …}           # Sessions (Identität dev)
     reviewer: {model: …}          # Audit (Identität reviewer)
     bot_login: my-builder-app     # Pflicht: Status-Kommentar nur von dieser Identität vertrauen; Commit-Autor
   ```

3. **Dispatcher ohne Agent.** `relay-dispatch` (cron, worktree-frei) prüft alle offenen
   Relay-Issues deterministisch und startet höchstens **einen** Run pro Projekt über
   `POST /api/runs` (`vars.issue_number`, `note: relay #N sK`). Gewählt wird der fällige Task,
   der am längsten wartet. Ist nichts fällig, entstehen keine Agent-Kosten. `not_before` im Handoff
   verschiebt die nächste Session (Rate-Limits, abgelaufene Credentials, externe Jobs).
   Die „ein Run pro Projekt“-Prüfung scannt die **komplette** Run-Historie des Projekts auf
   aktive Relay-Runs (nicht nur die neuesten N — ein Fenster würde bei vielen Runs anderer
   Loops einen laufenden Relay-Run übersehen und einen zweiten starten).

4. **Session = checkout → Agent → checkin → publish.**
   - `checkout` prüft die Freigabe erneut, schreibt den Handoff fort und legt `.fabrik-session/`
     an (git-ignoriert): Brief, Deadlines, Scope.
   - Der Agent (`gate: false`) arbeitet in kleinen Einheiten, protokolliert alles und aktualisiert
     den Handoff nach jeder Einheit; ein Kill verliert höchstens eine Einheit.
   - `checkin` repariert den Handoff, setzt Harness-Felder zurück, führt das Verify-Kommando aus
     (bei ungültigem Handoff entfällt es — der Task ist ohnehin sofort `blocked`), erkennt
     Stillstand (2 Sessions ohne Änderung der Evidenz → `blocked`) und Änderungen außerhalb
     des Scopes; letztere werden **verworfen** (tracked → HEAD, untracked → gelöscht), damit
     nach dem Entblocken nicht dieselben Pfade erneut blockieren.
   - `publish` committet nur Relay-Verzeichnis und `scope` (Autor und Committer = Bot aus
     `bot_login`), pusht als Builder, spiegelt den Status als Label
     (`relay: blocked`, `relay: done`) und aktualisiert **den einen Status-Kommentar**. Bei `done`
     zeigt er den `## Result`-Abschnitt des Handoffs, nach dem Audit dessen Zusammenfassung
     (`## Audit`, nur von `audit-publish` geschrieben; der Check-in setzt Agent-Änderungen daran
     zurück). Ein eigener Ergebnis-Kommentar entfällt.

   Ein Mensch entblockt, indem er das Label entfernt; das ist auch die Freigabe von Checkpoints.

   Legt `publish` den Status-Kommentar neu an, prüft es dessen Autor gegen `bot_login`. Weicht er
   ab (falsch konfiguriert), wird der Kommentar gelöscht und der Step scheitert mit `environment`;
   sonst fände der Dispatcher den Kommentar nie wieder, legte jeden Tick einen neuen an und verlöre
   den Zustand (`failures_seen`, `verify_pending`).

5. **Fixierte Invarianten.** Die erste Session pinnt den Hash von `## Verify`. Ändert sich der
   Abschnitt, blockiert der Task. Entfernt ein Mensch danach das Label, wird die neue Fassung
   übernommen. Weder Agent noch nachträgliche Edits verschieben so unbemerkt das Ziel.

6. **Unabhängiges Audit bei `done`.**
   - `relay-audit` läuft mit der Reviewer-Identität und einem anderen Modell.
   - **Phase 1, blind:** Alle Branch-Dateien sind ausgeblendet. Der Prüfer prüft jede Invariante
     mit eigenen Queries (Counts, Anti-Joins, Chunk-Checksummen, selbst gezogene Stichprobe) und nur
     mit dem read-only Zugang aus `## Audit access`.
   - **Phase 2:** Die Skripte und Logs des Workers werden gegen die eigenen Messungen geprüft.
   - Der Prüfer schreibt nur den Report: `audit-publish` setzt den Handoff auf den Stand aus
     `audit-prepare` zurück und verwirft alle Änderungen außer `AUDIT.md` und `audit/`.
   - Report `AUDIT.md` mit Verdikt-Marker. `pass` setzt `relay: audited`; `fail` oder ein fehlender
     Report setzt den Task zurück auf `blocked`.
   - Das Verdikt ist beratend; ein Mensch zeichnet ab, etwa per Stichprobe im Zielsystem oder als
     PR, falls Code entstand.

7. **Autorisierung des Briefs.** Ein Brief ist eine Anweisung an einen Agent mit Shell auf dem
   Host. Dispatcher und `checkout` führen ihn nur aus, wenn **das Opt-in-Label gesetzt ist**, **von
   einem `allowed`-Login gesetzt wurde** und die **aktuelle Body-Fassung von einem `allowed`-Login
   stammt**. Ein entferntes Label hält den Task an, auch für einen von Hand gestarteten Run. Ein Edit durch andere gilt erst, wenn das Label danach erneut gesetzt wird. Den
   Status-Kommentar liest der Dispatcher nur von der Builder-Identität.

8. **Wiederholung nach dem Run-Retry (ADR 0036).** `relay.retry` hat dieselbe Form und
   Bedeutung wie `retry` in ADR 0036 (geteiltes Modul `src/fabrik/retry.py`), Default aus:

   ```yaml
   retry:
     max: 3                    # zusätzliche Versuche nach dem gescheiterten Run (höchstens 4 Runs)
     backoff: [5m, 30m, 2h]    # Wartezeit vor Versuch 1..n, der letzte Wert wiederholt sich
     "on": [interrupted, transient]
     max_wait: 24h             # Obergrenze für ein gemeldetes retry_after
   ```

   - **Fehlschlag** = ein Relay-Run, dessen `publish`/`audit-publish` nicht durchlief (nichts auf
     dem Branch). Ein Agent-Timeout mit erfolgreichem `publish` ist normaler Betrieb; Stillstand
     erkennt der Check-in.
   - **Klasse** = `failure_kind` des Runs (ADR 0035). Nur Klassen aus `on` werden wiederholt.
     `unknown`, `environment`, `outcome` und Runs ohne Klasse (Altbestand) blockieren sofort.
     Das folgt ADR 0035: `unknown` heißt „nicht automatisch behandeln“.
   - **Die Relay-Steps klassifizieren an der Quelle** über den Exit-Code (ADR 0035): 75
     (`EX_TEMPFAIL`) = `transient` (Netz, GitHub-5xx, Rate-Limit), 78 (`EX_CONFIG`) = `environment`
     (401/403, falsches `bot_login`), sonst 1 = `unknown`.
   - **Backoff ist unabhängig vom Dispatcher-Takt.** Der Dispatcher prüft nur, ob
     `next_attempt_at` erreicht ist: `Ende des letzten Fehlschlags + backoff[n]`, nach hinten
     verschoben durch ein gemeldetes `retry_after` (höchstens `Ende + max_wait`, ADR 0036). Ohne
     Endzeit blockiert der Task (fail closed). Der Takt bestimmt die Auflösung, nicht den Abstand.
   - **Zustand ohne eigene Kette:** Die Versuche ergeben sich aus der Run-Historie (gescheiterte
     Runs des Tasks seit dem letzten Erfolg). Der Status-Kommentar speichert nur die Run-Id des
     letzten quittierten Fehlschlags.
   - Nach `max` Wiederholungen: `blocked`. Das Entblocken quittiert alle bisherigen Fehlschläge,
     die Zählung beginnt neu.

9. **Historie in drei Schichten, ohne Doppelung:**

   | Schicht | Inhalt |
   |---|---|
   | Status-Kommentar | aktueller Stand (in-place editiert) |
   | Branch | dauerhafte Geschichte, für das Team sichtbar |
   | Run-Historie | Betrieb: Dauer, Timeouts, Logs; durchsuchbar über `relay #N` in Notiz und Step-Summary |

## Begründung

- **Kurze Sessions statt langer Runs:** passen zu Dead-Man's-Switch, Neustarts und einem
  einzelnen Worker. Ein Abbruch kostet höchstens eine Einheit.
- **Branch statt lokalem State:** für das Team sichtbar und überlebt den Rechner. Ein Mensch kann
  den Branch auschecken, lokal (z. B. Claude Code) eine Session nach demselben Protokoll fahren und
  zurückgeben.
- **Deterministische Harness, Agent nur für die Arbeit:** Freigabe, Stillstand, Scope, Pins und
  Fehlerbremse entscheidet Code, nicht das Modell. Der Agent liefert Behauptungen, die Harness
  Evidenz.
- **Blindes Audit:** Zwei unabhängig geschriebene Prüfungen, die übereinstimmen, sind Evidenz. Wer
  zuerst den Worker-Code liest, übernimmt dessen Fehler.

## Abgelehnte Alternativen

- **Ein langer Run mit hohem Timeout:** scheitert an `FABRIK_STALE_HOURS` und Neustarts und
  blockiert den Worker.
- **Handoff lokal im fabrik-State:** unsichtbar für das Team, verloren mit dem Rechner.
- **Cron direkt auf `relay-session`:** startet jede Viertelstunde einen Agent, auch ohne fällige
  Aufgabe; Agent-Steps lassen sich nicht bedingt überspringen.
- **Ein Kommentar pro Session:** zu laut. Die Geschichte gehört auf den Branch, der Kommentar zeigt
  den Stand.
- **Audit durch Lesen der Worker-Skripte allein:** prüft Plausibilität, nicht das Ergebnis.
- **Eigene Retry-Kette (Datensatz) wie in ADR 0036:** Der Auftrag ist der Task, nicht der Run.
  Versuche lassen sich aus der Run-Historie ableiten; eine zweite Quelle wäre redundant. Die
  Semantik (`max`, `backoff`, `on`) ist trotzdem dieselbe.
- **Nächster Dispatcher-Tick als Retry:** wiederholt ohne Backoff und ohne Bezug zur
  Fehlerklasse; widerspricht ADR 0035 (`unknown` nicht automatisch behandeln).

## Konsequenzen

- (+) Neues Werkzeug für jedes Projekt: ein `relay:`-Block genügt.
- (+) Erfolg wird an fixierten Invarianten und unabhängig geprüft; Ad-hoc-Arbeit bleibt
  nachvollziehbar.
- (−) Sicherheit hängt an der Allowlist und an `bot_login`. Der Agent läuft weiterhin ohne Sandbox
  als OS-User (ADR 0010 Vorbedingung); Harness-Prüfungen schützen vor Drift, nicht vor einem
  böswilligen Agent.
- (−) Read-only Zugang für den Prüfer muss das Projekt bereitstellen; fabrik kann ihn nicht
  erzwingen. Ohne ihn bricht das Audit ab und meldet das.
- (−) Ein Relay-Run pro Projekt gleichzeitig; mehrere Tasks laufen reihum.
- Session-Länge (`max_session_minutes`) plus Audit-Timeouts müssen unter `FABRIK_STALE_HOURS`
  bleiben.

## Offene Fragen

- Soll `done` optional einen PR öffnen, wenn `scope` Code enthält?
- Wenn der Run-Retry (#290, ADR 0036) umgesetzt ist: dürfen Relay-Loops zusätzlich ein `retry`
  haben? Nein. Die Wiederholung gehört dem Task, sonst entstehen Doppel-Runs. ADR 0036 erlaubt
  `retry` auch auf Projekt-Ebene; die Umsetzung von #290 muss die generierten Relay-Loops davon
  ausnehmen (bzw. ein Loop-`retry` auf ihnen ablehnen).
- Run-Retention: Die Run-Historie wird heute nicht aufgeräumt. Mit dem Run-Store (ADR 0034) braucht
  es eine Regel, die Relay-Runs lange genug behält.

## Nachtrag 2026-11-03: Archiv des Audit-Trails, Trail aus dem PR entfernen

**Kontext.** Der Trail (`.fabrik/relay/<N>/`: Handoff, Skripte, Session-Logs, `AUDIT.md`,
`audit/`) liegt nach Entscheidung 1 auf dem Branch `fabrik/relay/<N>`. Entsteht dabei Code mit PR,
landet der Trail im PR-Diff und mit dem Merge auf `main`. Bei den ersten produktiven Tasks
(example-org/example-repo#1159 → PR #1160, example-org/example-repo#312 → PR #313)
waren 42 bzw. 41 von 48 Dateien Trail. Die PR-Beschreibungen versprachen „wird vor dem Merge
entfernt“, einen Mechanismus dafür gab es nicht; ein Squash-Merge übernimmt alle Dateien. Zugleich
muss der Trail erhalten bleiben: Er ist die Evidenz, auf die sich das Audit-Verdikt bezieht, und ein
erneutes Audit oder eine weitere Session braucht ihn. Bei Daten-Tasks kann er außerdem
Abfrageergebnisse und Stichproben aus Produktivdaten enthalten, die nicht in ein Git-Repo gehören.

**Entscheidung.**

1. **Zwei getrennte Schritte: archivieren, dann entfernen.**
   - *Archivieren* legt den Trail dauerhaft ab. Wie und wo, hängt vom Anbieter und von den Daten ab
     und ist austauschbar.
   - *Entfernen* ist ein normaler Lösch-Commit von `.fabrik/relay/<N>/` auf dem Relay-Branch
     (Weg wie jeder Relay-Push, also auch `commit_via: api`). Er ist für alle Anbieter gleich.
   - **Entfernt wird nur nach erfolgreichem, verifiziertem Archiv.** Ohne Archiv bleibt der Trail auf
     dem Branch; der Status-Kommentar warnt dann, dass der PR den Trail noch enthält.

2. **Archiv-Methode ist ein Pflicht-Interface.** Jede Methode implementiert:
   - `archive(issue, commit) → TrailRef` mit `uri` (wo der Trail liegt), `sha256` (Hash über den
     Trail-Inhalt, kanonisch: sortierte Pfade + Dateihashes), `commit` (Stand des Branches) und
     Zeitpunkt
   - `restore(ref) → Dateien` (stellt `.fabrik/relay/<N>/` wieder her)
   - `exists(ref) → bool`

   Eine Methode muss **unveränderliche** Ablage zusagen (kein Überschreiben eines vorhandenen
   Archivs); jedes Audit erzeugt ein neues Archiv mit fortlaufender Nummer `audit-<k>`.

3. **Das Verdikt bindet an den Inhalts-Hash, nicht an den Ablageort.** `audit-publish` schreibt
   `sha256` des geprüften Trails in den Status-Kommentar (Zustandsblock) und in `AUDIT.md`. Ein
   wiederhergestellter Trail wird gegen diesen Hash geprüft; weicht er ab, scheitert der Step
   (`environment`) statt mit verändertem Trail weiterzuarbeiten. Damit ist es für die Nachweisbarkeit
   gleich, ob der Trail in einem Git-Ref, in S3 oder lokal liegt.

4. **Ablauf.**
   - `audit-publish` mit Verdikt `pass` und konfigurierter Methode: Audit-Commit pushen →
     `archive` → `exists` + Hash prüfen → Lösch-Commit pushen → Status-Kommentar mit Link
     (`uri`), Hash und Archiv-Nummer.
   - Ist ein Audit oder eine Session fällig und fehlt `HANDOFF.md` auf dem Branch, während der
     Zustandsblock ein Archiv nennt: zuerst `restore` + Hash-Prüfung, als Commit auf den Branch,
     dann wie bisher. Ohne lesbares Archiv blockiert der Task.
   - `verdict=fail` archiviert und entfernt nichts; der Trail bleibt für die nächste Session auf dem
     Branch.

5. **Konfiguration** im `relay:`-Block, optional:

   ```yaml
   relay:
     trail_archive:
       type: git-ref        # git-ref | s3 | local
       # s3: bucket, prefix, region (Credentials aus der Umgebung des Hosts)
   ```

   Fehlt der Block, wird nicht archiviert und nicht entfernt (Verhalten wie bisher, plus Warnung im
   Status-Kommentar, wenn ein PR vom Branch existiert).

6. **Methoden.**
   - `git-ref` (erste Umsetzung, GitHub): versteckter Ref `refs/fabrik/relay/<N>/audit-<k>` auf den
     auditierten Commit, angelegt über `POST /git/refs`. Erscheint nicht in Tag- oder Branch-Listen,
     wird von `git clone`/`git fetch` ohne expliziten Refspec nicht geholt, löst keine
     Tag-Workflows aus und hält den Commit auch nach dem Löschen des Branches erreichbar. `uri` ist
     der Commit-Link (`…/tree/<sha>`). Am 2026-11-03 auf `didaskomathetes/quark-playground` per
     API und `git push` getestet; in der `example-org`-Org vor dem ersten Einsatz einmal zu prüfen.
   - `local` (erste Umsetzung, Entwicklung/Tests): Archiv als Verzeichnis unter
     `FABRIK_DATA/relay-archive/<projekt>/<N>/audit-<k>/`. Nicht für Teams geeignet: nur auf diesem
     Rechner.
   - `s3` (vorgesehen, Daten-Tasks): `s3://<bucket>/<prefix>/<projekt>/<N>/audit-<k>.tar.gz` mit
     Manifest (Commit, Hash, Verdikt). Unveränderlichkeit über S3 Object Lock bzw. eine
     Bucket-Policy ohne Überschreiben; Verschlüsselung und Aufbewahrung regelt der Bucket.
   - Weitere Anbieter (GitLab, …) kommen als eigene Methode dazu. Ob sie versteckte Refs erlauben,
     ist pro Anbieter zu prüfen; sonst bleibt `s3` oder `local`.

**Begründung.**
- Das Risiko ist der Trail im PR-Diff; dafür genügt ein Lösch-Commit. Was austauschbar sein muss,
  ist allein der Ablageort, also wird nur dieser abstrahiert.
- Archiv vor Entfernen und Hash-Bindung verhindern, dass Evidenz still verloren geht oder nach dem
  Audit unbemerkt verändert wird.
- Daten-Tasks brauchen einen Ablageort außerhalb von Git (Zugriffsrechte, Verschlüsselung,
  Aufbewahrung); deshalb ist S3 keine Notlösung, sondern die vorgesehene Methode dafür.

**Abgelehnte Alternativen.**
- **Tags** (`fabrik-relay/<N>/audit-<k>`): sichtbar in Tag- und Release-Listen, stören Werkzeuge,
  die alle Tags sortieren, und lösen `on: push: tags`-Workflows im Zielrepo aus.
- **Archiv-Branch**: veränderlich und erscheint in der Branch-Liste.
- **GitHub Action im Zielrepo, die `.fabrik/` vor dem Merge entfernt**: muss in jedes Team-Repo,
  hängt vom Anbieter ab, und die Evidenz geht mit dem Merge verloren.
- **Trail von Anfang an auf einem separaten Orphan-Branch**: sauberster PR, aber Umbau von
  Entscheidung 1 (Handoff-Ort, Push, Checkout, Audit lesen aus zwei Branches). Bleibt eine Option
  für später.
- **Nur die Commit-Historie** (Trail ist Parent des Lösch-Commits, GitHub hält ihn über
  `refs/pull/<PR>/head`): nur mit PR, nur bei GitHub, kein stabiler Name.

**Konsequenzen.**
- (+) PR-Diff und Merge enthalten nur die eigentliche Änderung; die `ruff.toml` im Relay-Verzeichnis
  wirkt nur noch bis zum Entfernen.
- (+) Das Verdikt ist über den Hash an genau den geprüften Trail gebunden, unabhängig vom Ablageort.
- (−) Ein Schritt mehr in `audit-publish` und ein Wiederherstellungs-Pfad in Dispatcher/Checkout.
- (−) `s3` braucht Schreibrechte des Hosts auf einen Bucket; das verschärft das offene
  Sandbox-Thema (Agent läuft als OS-User mit Zugriff auf Cloud-Credentials).
- (−) Versteckte Refs sind in der GitHub-Oberfläche nur über den Link im Status-Kommentar, die API
  oder `git ls-remote` zu finden.

**Offene Fragen.**
- Aufbewahrung: Wie lange bleibt ein Archiv (Ref, S3-Objekt), und wer räumt auf?
- Soll ein Projekt ohne `trail_archive` einen PR vom Relay-Branch überhaupt öffnen dürfen?
- Verhalten weiterer Anbieter (GitLab: versteckte Refs vermutlich nur per `git push`, nicht per
  API; ungeprüft).
