# Code-Review: Software-Fabrik PR #10 (v0.3/P0) + PR #11 (v0.4/P1)

Basis: `fabrik/fabrik.py` auf `p1/agent-backend` (1744 Zeilen), Selftest-Spec, README, Spike-Doku.
Alle Zeilenreferenzen beziehen sich auf `fabrik/fabrik.py` im P1-Stand. Subagent-Review, 2026-10-15.

---

## CRITICAL

**C1 — LLM-Step vergiftet ctx mit allen Response-Keys → Überschreiben vertrauenswürdiger Variablen (Untrusted Input → Execution Path)**
`fabrik.py:961-962` — `for k, v in parsed.items(): ctx[k] = ...` injiziert **jede** Key aus der LLM-Antwort in den Kontext, nicht nur Schema-Keys. Die LLM-Prompts enthalten Untrusted-Input (`untrusted: [issue_title, issue_body]`, `page`), d. h. ein präparierter Issue-Body kann die LLM dazu bringen, zusätzliche Keys zu emittieren: `{"class":"x","workdir":"/etc/cron.d","root":"/"}`. Ab dann: Report-Step schreibt auf `root`-relativer Basis an beliebige Pfade (C2), Agent-Step arbeitet im `workdir`-Default `ctx["workdir"]` (`fabrik.py:821`) mit `bash "*` — **ohne dass zwischenzeitlich eine Approve-Stage greift** (Tier-Gate liegt am Loop-Ende). Das verletzt Leitprinzip 2 direkt: Untrusted-Input hat einen Execution-Path.
*Fix:* Nur Schema-Keys in ctx injizieren (Allowlist aus `schema`), bestehende ctx-Keys nie überschreiben, alternativ Namenspräfix (`llm_<step>__<key>`).

**C2 — Path Traversal via LLM-Ausgabe in `report.write` → beliebiges Datei-Schreiben als root**
`fabrik.py:513-520` + `selftest.fabrik.yaml:45` — `write: reports/{run_id}-{class}.md`, wobei `class` ein **unbegrenzter String** ist (Schema prüft nur `type: string`, kein Enum, keine Längengrenze, `fabrik.py:877-896`). LLM-Ausgabe `class: "../../etc/cron.d/evil"` → Pfad `reports/<id>-../../etc/cron.d/evil.md` → landet außerhalb der Projekt-Root; Content ist LLM-Text, der Untrusted-Webdaten enthält. Selbst ohne C1 (bereits LLM-Halluzination reicht).
*Fix:* Nach Substitution: `p = (root / p).resolve()` und prüfen, dass `root.resolve() in p.parents` (keine `..`/absolute Escape); Schema um `enum`/Länge erweitern.

## HIGH

**H1 — Agent-Permissions-Default = unbegrenzte root-Ausführung, einziger Schutz ist eine Prompt-Anweisung**
`fabrik.py:662-667` — Default `permissions` = `edit pattern:*` + `bash pattern:*`. Das Agent-Modell läuft als root mit unbegrenztem Shell-Zugriff; `pattern:"*"` ist **nicht** auf `workdir` beschränkt (opencode-Edit-Pattern matchen Dateipfade, `*` = ganzes Dateisystem). Die Untrusted-Markierung im Prompt (Leitprinzip 2) ist nur LLM-level-Weiche — keine harte Grenze.
*Fix:* Default-Deny; `pattern` auf `workdir` scoped (z. B. `**` relativ zur Session-Directory), Bash-Allowlist; Workdir-Containment in der Doku als harte Voraussetzung deklarieren.

**H2 — `poll()`: falsch-frühes "done" + keine Failure-Detektion (SPIE-Interface bricht `failed`-State ab)**
`fabrik.py:682-704` — Done-Heuristik: letzte Assistant-Message hat nicht-leeren Text (`fabrik.py:702`). Das ist **kein** Abschluss-Signal: Modell, das zwischen Tool-Calls Narrations-Text emitte → "done", während die Session noch läuft → Conductor vertraut dem Step-Outcome, während Dateien noch geschrieben werden (Prinzip 3 gebrochen). Und: die Spike-Doku definiert das SPIE-Interface als `running|done|failed` (`spike-opencode.md`), implementiert sind nur `running|done`. Abgerissene/leere Sessions (Spike-Pitfalls #1/#7, dokumentiert!) = "running" bis zum Step-Timeout (Default 1800 s = 30 min). Die API liefert `finishReason`/Completion-Infos (steht in der Spike-Doku) — ungenutzt.
*Fix:* Done via `info.finishReason != null` bzw. `time.completed`; `failed`-State aus Session-/Message-Error mappen; Timeout-Heuristik nur Fallback.

**H3 — Keine Session-Cleanup auf Fehlerpfaden → verwaiste opencode-Sessions**
`fabrik.py:828-836`, `706-711` — `cancel()` wird nur im Timeout-`else` aufgerufen, best-effort mit verschluckter Exception (Server tot → leiser Orphan). Schlimmer: wenn `poll()`/`send_prompt()`/`create_task()` mid-Run eine Exception werfen (Server-Neustart, OOM, 15-s Request-Timeout), propagiert sie nach `run_loop` (`fabrik.py:461`) — **die erstellte Session wird nie abgebrochen** → orphaned Session, die weiter auf dem Modell rechnet (Kosten + Ressource). Kein `try/finally` über die Session-Lebensdauer.
*Fix:* `try/finally` um create→poll→done; `cancel()` in jedem Exit-Pfad; Cancel-Fehler loggen, nicht swallowen.

**H4 — Secret-Masking ist lückenhaft: Run-JSON nie redactiert, Token-URLs landen in den Records**
`fabrik.py:311-324` — `_redact()` greift **nur** in `emit()` (`fabrik.py:384`, d. h. nur `.log`). Die Run-JSON-Records werden nie redactiert: `step["summary"]/["output"]` (`fabrik.py:446-451`), `record["error"] = str(e)` (`fabrik.py:487`) → via `/api/run/{id}` im UI lesbar. Konkret: `git fetch`-Fehler mit Token-URL — `git` echoed die URL inkl. `x-access-token:TOKEN@github.com` im `fatal:`-Text → `RuntimeError(... {p2.stderr})` (`fabrik.py:184-189`) → Token im Run-JSON. Zusätzlich: GITHUB_TOKEN aus `/root/.hermes/.env` kommt **nicht** in `os.environ` (`fabrik.py:171-179`) → wird auch im `.log` nicht gemaskt (nur Env-Vars gescannt, `fabrik.py:314-317`). Und Spec-`env:`-Secrets (`fabrik.py:400`) werden nie gemaskt — der Shell-Step emitet den voll substituierten Befehl (`fabrik.py:537`).
*Fix:* Record-Felder (summary/output/error) vor dem JSON-Write redactieren; alle Secret-Quellen (env, Env-Dateien, Spec-env) in `_secrets()` registrieren; git-Fehlermeldungen vor dem Raise durch `_redact()`.

**H5 — `on_issue` liest nur die erste API-Seite → neue Issues unsichtbar ab >50 offenen Issues**
`fabrik.py:1111` — `?state=open&per_page=50` ohne Sortierung → GitHub liefert `sort=created&direction=asc` (älteste zuerst). Neue Issues liegen auf Seite 2+ → `ids` enthält sie nie → Trigger feuert nie. Im Selftest-Repo (<50 offen) unauffällig, im Produktivbetrieb gebrochen.
*Fix:* `&sort=created&direction=desc` (neueste zuerst).

**H6 — Resume synct den Worktree neu → zerstört vor der Approve erstellte Arbeit**
`fabrik.py:390` + `222-228` — Bei Resume (`approve`) läuft `ensure_worktree` erneut: `git reset --hard origin/<branch>` verwirft vor-Approval-Commits; hat ein Step im Worktree einen **Branch** angelegt (`cur != branch`), wird der Worktree per `worktree remove --force` **gelöscht** und das Resume wirft RuntimeError → der freigegebene T2-Run scheitert nach der Approval.
*Fix:* Bei Resume keinen Worktree-Sync/Reset (Status beibehalten) oder nur Fetch ohne Hard-Reset.

## MEDIUM

**M1 — Stale-Run-Deadman flippt laufende Records → False-Positive-Alerts, zwei Writer auf ein JSON**
`fabrik.py:1137-1166` — Ein Run mit mehreren 1800-s-Agent-Steps übersteigt legitim die 6-h-Default-Schwelle; das Scheduler-Thread flippt `status: error` **während der Run-Thread noch läuft** (False-Positive-Alert), und beide schreiben auf dieselbe JSON-Datei ohne Lock/atomaren Replace; der finale Write des Run-Threads überschreibt dann den Flip.
*Fix:* Schwelle pro Step-Typ skalieren bzw. nur flippen, wenn zugehöriger Thread tot ist (PID/Heartbeat-Check); atomar schreiben (temp+rename).

**M2 — http-Step: `file://`-Reads und SSRF-Redirects möglich, keine Size-Cap**
`fabrik.py:846-874` — `urllib.request.urlopen` unterstützt `file://` → lokale Dateien (als root) landen in ctx/Reports; Redirects werden gefolgt (inkl. https→http-Downgrade, Loopback wie `127.0.0.1:8321` = die eigene API, `169.254.169.254` = Cloud-Metadata); keine Scheme-/Host-Allowlist; `r.read()` ohne Größenlimit.
*Fix:* Scheme-Whitelist (http/https), Loopback/Link-Local-Block oder Allowlist, Redirects auf interne Ziele verbieten, Body-Cap.

**M3 — Tier-Gate wird bei vorhandenem Step-Approval komplett übersprungen — frühe Approve deckt ganzen Loop**
`fabrik.py:468-483` — `not any(s.get("approval") for s in loop["steps"])`: hat **irgendwo** im Loop ein Step `approval: true`, greift das Tier-Gate nicht. Approve am Anfang des Loops befreit dann alle späteren, potenziell irreversiblen Steps von jeglicher Kontrolle — das Design-Prinzip "T2+ endet mit Approval" gilt nur ohne Step-Approvals.
*Fix:* Tier-Gate immer anhängen (oder: Approval-Position muss nach den T2-Sink-Steps liegen).

**M4 — Trigger-Events werden bei besetztem Projekt-Lock still verworfen**
`fabrik.py:1169-1178` — `_safe_run` gibt bei `lock.acquire(blocking=False)`-Fehlschlag einfach auf: kein Queue, kein Retry, kein Log-Eintrag im Record. `on_issue`/`cron`/`on_file`-Events gehen verloren.
*Fix:* Event-Queue pro Projekt oder expliziter "dropped"-Log.

**M5 — Health-Gate nur `/api/health` — Spike-Empfehlung (E2E-Ping) nicht implementiert**
`fabrik.py:655-660` — Spike-Doku (Pitfall #5): "Health-Gate muss E2E-Requests prüfen (nicht nur den Port)" — wedged Ollama-State passt `/api/health`. Die Readiness-Warte-Loop (`fabrik.py:814-818`) erbt das.
*Fix:* Health = Port-Check + kleiner E2E-Prompt mit Zeitlimit.

**M6 — `github_issue`-Notification + `on_issue`-Loop auf Label `fabrik` = selbst-triggerrnder Run-Loop**
`fabrik.py:754-768` + `1096-1135` — Jeder fehlgeschlagene Run eröffnet ein Issue mit Label `fabrik`; beobachtet ein `on_issue`-Loop dieses Label, startet jede Failure-Notification einen neuen Run → bei persistentem Fehler unendliche Run-Kaskade (jedes neue Issue-Number ist "new").
*Fix:* Eigene Issues filtern (erstellendes User-Konto checken) oder `fabrik-`-Label-Präfix von on_issue ausschließen.

**M7 — `initial_vars` werden zuletzt gemerged → können vertrauenswürdige ctx-Keys überschreiben**
`fabrik.py:400-403` — Merge-Reihenfolge: Base-ctx (`project`, `root`, `workdir`, `run_id`) → env → `root` → `ctx.update(initial_vars)`. CLI `--var workdir=/x` (`fabrik.py:1615-1625`) überschreibt `workdir` direkt; geplante Webhook-Trigger füttern denselben Merge mit untrusted Daten.
*Fix:* initial_vars zuerst mergen / reservierte Key-Namen (`root`, `workdir`, `run_id`, `project`) ablehnen.

**M8 — Test-Abdeckung: null**
Kein Test-File unter `fabrik/` (`git ls-tree` bestätigt). Nicht getestet: Resume-/Tier-Gate-Logik, Schema-Validierung, `_redact`, `_check_stale_runs`, `on_issue`-State, Agent-Poll/Fehlerpfade, http-Step, `_substitute`. Einzige Verifikation = Selftest-Spec (Integration, kein Unit-Test). Gerade die P1-Fehlerpfade (H2/H3) sind damit vollständig ungedeckt.

**M9 — agent-smoke-Verify kann auf veralteter Datei grün werden**
`selftest.fabrik.yaml:87-88` — `test -f /tmp/fabrik-agent-smoke/smoke.txt`: bleibt von einem früheren Run erhalten → Verify passt, obwohl der Agent in diesem Run nichts gemacht hat (genau der dokumentierte Silent-Failure-Modus, Spike #1/#7). Kein Cleanup des Workdirs vor dem Run.
*Fix:* Workdir zu Run-Start löschen (rm -rf + fresh) oder `run_id`-spezifische Datei prüfen.

**M10 — README-Beispiel nutzt tote Felder; P1-Felder ohne Spec-Validierung**
`fabrik/README.md` (Loop-Beispiel): `gate: true` und `prompt_file` — existieren im Code nicht (grep bestätigt: kein `gate`-Handling). Eine nach README geschriebene Spec läuft **ohne Approve-Gate**. Umgekehrt: P1-Neufelder (`backend`, `workdir`, `endpoint`, `model`, `permissions`, `timeout` beim agent-Step) gehen durch `_validate_spec` (`fabrik.py:103-160`) unvalidiert — Typos/Typfehler fallen erst zur Runtime.
*Fix:* README-Beispiel auf aktuelle Felder; Spec-Validierung um P1-Felder (Typen, `backend` ∈ {opencode, …}, `timeout` numerisch).

## LOW

- **L1:** `tier: true` (YAML-Bool) gilt als T1 — `isinstance(True, int)` (`fabrik.py:122-124`); Schema-Typ `number` akzeptiert Bool (`fabrik.py:894`); Cron `5/15` feuert nur Minute 5 statt 5/20/35/50 (`fabrik.py:1307-1312`); `/api/runs?limit=abc` → unbehandeltes ValueError (`fabrik.py:1546`); `run_id` auf Sekundenauflösung → Kollision/Über schreiben bei gleichem Loop im selben Tick (`fabrik.py:162-165`); `Access-Control-Allow-Origin: *` (`fabrik.py:1431`, **präexistent**) erlaubt Drive-by-Calls aus beliebigen Webbseiten im Browser — inkl. `/api/approve` ohne CSRF-Schutz; `steps_output` in `list_runs` ist toter Code (`fabrik.py:1585-1587`); Reject/Approve-Race-Fenster zwischen Chat-Handler und CLI-Resume.
- **L2 (verifiziert, kein Bug):** `_substitute` ist Single-Pass (`fabrik.py:298-301`) — keine Substitutions-Loops, kein Code-Eval über Var-Namen; Quote-JSON in Prompts (`{"class": ...}`) überlebt, da Zitate nicht im `[a-zA-Z0-9_]`-Match. while/else-Timeout-Semantik korrekt (`fabrik.py:828-836`), Overshoot bounded (~45 s).

---

## Top-Risiken (Kurzform)

1. **C1+C2:** Untrusted Issue-Content → LLM-Ausgabe → ctx-/Pfad-Manipulation → Datei-Write/Agent-Execution als root, **vor** der Tier-Approval. Einziger Schutz ist Prompt-Text.
2. **H2:** Agent-Step liefert falsch-frühe "done" und blendet Session-Failures aus → falsche Greens bei dem genau dem Mechanismus, dem der Conductor laut Prinzip 3 vertrauen soll.
3. **H1:** `bash/edit "*"` als root — Prompt-Injection hat eine harte Execution-Oberfläche ohne Containment.
4. **H4:** Token in Run-JSONs (git-Error-URLs) + blinde Flecken für File-/Spec-env-Secrets → Secret-Leak über UI/API.
5. **H5+H6:** on_issue schweigend inaktiv ab >50 offenen Issues; Resume zerstört freigegebene Worktree-Arbeit — zwei Verlässlichkeitsbrüche, die im Selftest nie sichtbar werden.
