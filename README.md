# fabrik — die Software-Fabrik

Agentic Development-Loops als YAML-Specs definieren, lokal ausführen, per UI
steuern, per Chat anpassen. Ein Engine-Prozess (`fabrik serve`) beobachtet
Triggers und führt Loops aus; die UI auf `http://127.0.0.1:8321` zeigt
Projekte, Loops, Runs, Live-Logs und ein Chat-Interface.

**Prinzipien:**
- Lokal, 127.0.0.1, kein Cloud-Backend, kein Build-Step
- Python-Stdlib + PyYAML — ein Python-Paket (`src/fabrik/`) + vanille-JS-UI
- **Python 3.11+** (modernes Typing: `str | None`, `tuple[str, bool]`;
  `requires-python` in `pyproject.toml`)
- Git-Projekte laufen in **isolierten Worktrees** (der Haupt-Checkout wird
  nie angefasst)
- **Push nie auf geschützte Branches** (`main`/`master` = hart blockiert);
  alle Ergebnisse laufen als Branch + PR
- Ein Projekt = eine Spec; ein Loop = geordnete Steps; ein Run = JSON + Log
- **Autonomie mit Gates**: Loops laufen komplett selbstständig; nur
  explizit markierte kritische Steps (`approval: true`) pausieren den Run,
  bis ein Mensch freigibt

## Start

```bash
fabrik serve            # Engine + UI + Scheduler, 127.0.0.1:8321
fabrik list             # Projekte + Loops
fabrik validate         # Specs prüfen (--offline [SPEC…]: ohne root-Prüfung, z. B. CI)
fabrik run <projekt> <loop> [--note "…"]
fabrik runs [--project X]
fabrik runs show <lauf_id> [--project X] # Einzelrun: Felder, Steps, Session-Log
fabrik runs export <lauf_id> [--project X]  # Einzelrun + Session-Log als JSON
fabrik approve <lauf_id> [--project X]   # needs_approval → fortsetzen
fabrik reject  <lauf_id> [--project X]   # needs_approval → abbrechen
fabrik chat "…"          # Chat-Befehl / LLM-Copilot
fabrik alert test [--project X]          # Alert-Kanäle testen
```

**GitHub-Apps (ADR 0010):** `scripts/gh-token-refresh.py` erneuert die 1h-Installation-Tokens —
ohne Argument die Builder-App (`BUILDER_APP_*` → `GITHUB_TOKEN`), mit `--role reviewer` die
QA-App (`REVIEWER_APP_ID`, `REVIEWER_APP_KEY_FILE`, `REVIEWER_INSTALLATION_ID` →
`GITHUB_REVIEWER_TOKEN`). Je eine Cron-Zeile alle 30 min.

*GitHub-spezifisch (Implementierungsdetail, kein ADR; zieht mit den Provider-Ports aus PR #437
in den GitHub-Adapter um):*

**Tokens pro Projekt:** `github_env_file: <pfad>` in einer Spec ersetzt für dieses Projekt
`FABRIK_ENV_FILE`: Runs, Trigger-Polls, Retry-Versuche und deren Subprozesse (Shell-Steps,
Agent-`gh`) lesen `GITHUB_TOKEN`/`GITHUB_REVIEWER_TOKEN` nur aus dieser Datei; globale
`GITHUB_TOKEN`-Env-Werte gelten dort nicht (Shell-Steps bekommen `GH_TOKEN`/`GITHUB_TOKEN` =
Builder-Token des Projekts; Subprozesse erkennen den Override an `FABRIK_PROJECT_ENV_FILE`).
Für eine andere App-Installation (andere Org) oder
einen PAT als Reviewer. Refresh per Cron mit Env-Override, z. B.
`FABRIK_ENV_FILE=<pfad> BUILDER_INSTALLATION_ID=<id> scripts/gh-token-refresh.py`.

**Signierte Commits (GitHub):** `commit_via: api` in einer Spec (Default `git`) pusht nicht per `git push`,
sondern baut die lokalen Commits über die Git-Data-API nach (`src/fabrik/apicommit.py`). GitHub
signiert sie für die Builder-App selbst ("Verified", Autor = App-Bot) — für Repos mit
`required_signatures`, ohne Schlüssel auf dem Host. Gilt für `git`-Steps (`push: true`), die
Fix-Runden von `pr_review` und Relay-Pushs. Agents, die selbst pushen, rufen
`python -m fabrik.push [--force]` statt `git push` auf (Weg je nach Projekt über
`FABRIK_COMMIT_VIA`). Grenzen: kein Root-Commit; lokaler Autor/Zeitstempel gehen verloren.

`fabrik` ist das Console-Script aus `pyproject.toml` (nach `uv sync`).
Ohne Installation: `python -m fabrik …` (aus dem Projekt-Root).

Env-Varian: `GITHUB_TOKEN` (GitHub-API), `FABRIK_ENV_FILE` (Pfad zur
`.env`-Datei mit `GITHUB_TOKEN`, Default `/root/.hermes/.env`), `FABRIK_TOKEN`
(optional: Bearer-Token für die API-`POST`-Routes), `FABRIK_WORKERS`
(Worker-Pool-Größe, Default 4), `FABRIK_LLM_PROVIDERS` +
`FABRIK_LLM_<NAME>_{URL,MODEL,KEY}` (LLM-Provider, s. u.).

### Lokaler Betrieb (systemd-User-Units)

Dauerbetrieb läuft als User-Service statt Foreground-`fabrik serve`, damit
Scheduler/Cron-Trigger Terminal-Ende und Reboot überleben. Nur ein
`serve`-Prozess gleichzeitig (s. „Single-Node" unter Runs).

Die Units in `deploy/` enthalten keine Checkout-Pfade. Sie lesen `FABRIK_HOME`
(das `fabrik/`-Verzeichnis des Checkouts, gleiche Bedeutung wie in
`config.py`) aus **einer** Datei pro Maschine und wechseln per
`/bin/sh -c 'cd "$FABRIK_HOME" && exec …'` dorthin (systemd expandiert keine
Variablen in `WorkingDirectory=`). Fehlt die Variable, bricht die Unit mit
„FABRIK_HOME fehlt" ab.

```bash
# ~/.config/fabrik/deploy.env — einmal pro Maschine anlegen
mkdir -p ~/.config/fabrik
echo "FABRIK_HOME=$PWD/fabrik" > ~/.config/fabrik/deploy.env   # aus dem Repo-Root
# Beispiel-Inhalt: FABRIK_HOME=/home/<user>/quark-playground/fabrik
```

```bash
cp fabrik/deploy/fabrik.service fabrik/deploy/fabrik-autoupdate.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fabrik.service fabrik-autoupdate.timer
journalctl --user -u fabrik -u fabrik-autoupdate -f      # Logs
```

systemd startet ohne Shell-Profil: Agent-CLIs außerhalb von `/usr/bin`
(`copilot` via nvm, `claude`, `opencode`) fehlen sonst im `PATH` → Agent-Steps
scheitern mit „CLI '…' nicht im PATH". Maschinenspezifisch per Drop-in setzen:

```ini
# ~/.config/systemd/user/fabrik.service.d/path.conf
[Service]
Environment=PATH=%h/.nvm/versions/node/<version>/bin:%h/.local/bin:%h/.opencode/bin:/usr/local/bin:/usr/bin:/bin
```

`enable` nicht vergessen, sonst startet nach einem Reboot nichts (`systemctl --user is-enabled
fabrik`); ohne Login braucht es außerdem `loginctl enable-linger`.

**Auto-Update nach Merges** (`scripts/fabrik-autoupdate.py`, Timer alle 5 min):
zieht `main` per `--ff-only` von `origin` nach (nur auf Branch `main`; bei
lokalen Commits/Konflikten Abbruch mit Exit 1, nichts wird überschrieben),
führt bei geänderter `pyproject.toml`/`uv.lock` `uv sync` aus und startet
`fabrik.service` neu, wenn sich unter `fabrik/` seit dem Start-Commit
(`state/serve-commit`, geschrieben per `ExecStartPre`) etwas geändert hat.
Neugestartet wird nur im Leerlauf — laufen `running`/`queued`-Runs, wird auf
den nächsten Tick vertagt (ein Restart würde sie als `error`/`interrupted`
verwaisen lassen; Auto-Retry greift, wenn konfiguriert, ADR 0036).
`needs_approval`-Runs blockieren nicht.

## Entwicklung

`uv`-Projekt (Lock-File: `uv.lock`); pytest, ruff, pyright und bandit als Dev-Gruppe:

```bash
uv sync                 # venv + Abhängigkeiten (Runtime + Dev)
uv run pytest           # Unit-/Integrationstests
uv run pytest -m "not browser and not perf" -n auto   # parallel (pytest-xdist), deutlich schneller
uv run ruff format .    # Format
bash scripts/lint.sh    # Gate: ruff check + format --check, pyright (Typen), bandit (SAST, ab medium)
```

**Browser-Tests (UI):** `tests/test_ui_browser.py` startet einen isolierten
fabrik-Server + Chromium (Playwright) und prüft die interaktiven UI-Verhalten
(Scrollbars, Run-Modal, klickbare Loop-Specs, Chat-Overlay). Einmalig:
`uv run playwright install chromium`. Ausblenden: `uv run pytest -m "not browser"`.

**CI:** `.github/workflows/fabrik-ci.yml` (PRs + Pushes auf `main`, nur bei Änderungen unter
`fabrik/`) hat zwei unabhängige Jobs: `test` (`uv sync --frozen`, `scripts/lint.sh`
(ruff, pyright, bandit), `pytest -m "not browser and not perf" -n auto`, danach `pytest -m perf`
seriell) und `browser` (Playwright + Chromium, `pytest -m browser`). Python 3.11+, uv-Cache über `uv.lock`.

**Worker-Pool:** Trigger-Runs laufen auf einem bounded Pool von Daemon-Workers
(`FABRIK_WORKERS`, Default 4). Für lokale Entwicklung / Initial-Test:
`FABRIK_WORKERS=1` → alle Runs seriell auf einem einzigen Agent-Worker
(keine parallelen LLM-Sessions). Daemon-Threads: ein Ctrl+C (oder SIGTERM/SIGHUP) beendet den
Prozess sofort, ohne auf laufende Runs zu warten; laufende Shell-Steps und One-Shot-Agents
werden samt ihrer Prozessgruppe beendet.

**Parallele Runs (ADR 0028):** Jeder Run arbeitet in einem eigenen Git-Worktree
(`<WORKTREES_DIR>/<projekt>/<run_id>`; `FABRIK_WORKTREES`, Default
`~/.local/state/fabrik/worktrees/<installation>` — bewusst außerhalb des Checkouts, damit
IDEs die Run-Checkouts nicht indizieren, ADR 0047 Nachtrag), der am Ende (bzw. beim Reject, und nach
einem Crash beim nächsten Server-Start) aufgeräumt wird; ein Run in `needs_approval`
behält ihn für das Resume. `max_parallel` (Projekt-Spec, Default 3) begrenzt
gleichzeitige Runs pro Projekt; Specs ohne `repo:` laufen ohne Isolation im `root` und
damit fest mit `max_parallel: 1`. Trigger-Runs über dem Limit werden verworfen,
manuelle/Chat-Runs und freigegebene Resumes werden eingereiht (ADR 0015) und warten auf
einen freien Slot. `worktree: false`-Loops (ADR 0020) belegen keinen Slot. Pro Loop
+ Issue/PR läuft höchstens ein aktiver Run. `state/replied-comments.json` und
`state/reviewed-prs.json` werden gelockt in `state/agent/<projekt>/` zusammengeführt.
Betrieb: genau **ein** Fabrik-Prozess pro Projekt (siehe ADR 0028).
Echte Parallelität braucht `FABRIK_WORKERS` > 1.

## Spec-Format (`*.fabrik.yaml`)

```yaml
name: kyudo-neandertal
description: "Drupal 7→11 Migration"
root: /root/kyudo-neandertal
max_parallel: 3                             # gleichzeitige Runs (Default 3)
repo:
  branch: master
  subpath: drupal11
github: didaskomathetes/kyudo-neandertal   # für on_issue + github_issue-Alerts

notify:                                     # Alert-Kanäle bei (Default: run_failed)
  slack: https://hooks.slack.com/services/…
  webhooks:                                 # generisch: Datadog, CloudWatch, …
    - {name: datadog, url: https://http-intake.logs.datadoghq.com/v1/input?api_key=…}
  github_issue: true                        # bei run_failed: Issue im Repo öffnen
  issue_labels: [fabrik]
  events: [run_failed]                      # auch: [run_failed, run_passed]; run_failed schließt run_exhausted ein

loops:
  migrate:
    description: "Drupal-Änderungen als Branch + PR"
    triggers:
      - {type: on_issue, labels: [migration], poll: 300}   # neue Issue → Loop
    steps:
      - {type: git, name: prepare, action: prepare, message: "fabrik: …"}
      - {type: agent, name: changes, prompt_file: prompts/….md}
      - {type: shell, name: checks, cmd: "bash scripts/run_checks.sh"}
      - {type: pr, name: pr, base: master, title: "…", approval: true}
```

### Spec-Quellen, `vars` und Loop-Templates (ADR 0047)

**Wo Specs liegen** (erster Treffer gewinnt; gleiche `name:` in zwei Quellen meldet `fabrik validate` als Fehler):

1. `FABRIK_PROJECTS_PATH` — doppelpunkt-getrennt wie `PATH` (Ordner mit `*.fabrik.yaml` oder Repo mit `.fabrik/fabrik.yaml`)
2. `FABRIK_DATA/projects` (ohne `FABRIK_DATA`: `projects/` der Installation)
3. Zielrepos mit `.fabrik/fabrik.yaml`, registriert per `fabrik project add <repo-pfad>` (ohne `name:` gilt der Repo-Ordner, ohne `root:` das Repo)
4. die mitgelieferten Beispiele in `projects/`

**Pfade:** `prompt_file` und `scripts/…` in Shell-Befehlen werden relativ zur Spec-Datei aufgelöst, dann relativ zur
Installation (`prompts/…`, `scripts/…` der Fabrik — Skript-Namen daher eindeutig halten).

**`vars:`** — `{name}` gilt in der ganzen Spec (typgetreu, wenn der String nur `{name}` ist). Andere `{x}` bleiben
Laufzeit-Variablen (`{issue_number}`, `{root}`, …; als `vars`-Namen verboten). `vars` dürfen andere `vars`
referenzieren (`a: "{b}"`); Zyklen sind ein Fehler.

**Loop-Templates** (`templates/<name>.loop.yaml`; mitgeliefert: `develop`, `review-pr`): `use:` + `with:`.
Pflichtparameter (`null` im Template) kommen aus `with` oder `vars`; weitere Loop-Schlüssel überschreiben das Template,
`insert: [{after: <step>, steps: […]}]` fügt Steps ein (nicht zusammen mit eigenen `steps:`). Unbekannte `with`-Keys
(weder `params` noch Platzhalter im Template) sind ein Fehler. Ein zweites Projekt (Repo `acme/app`):

```yaml
name: app
root: ~/src/app
github: acme/app
vars: {repo: acme/app, builder_model: claude-sonnet-5-5, reviewer_model: gpt-5.6-luna}
loops:
  develop:
    use: develop
    with: {prompt_file: develop.prompt.md, lint_cmd: make lint, test_cmd: make test}
  review-pr: {use: review-pr, with: {prompt_file: review.prompt.md}}
```

Liegt die Spec als `.fabrik/fabrik.yaml` im Repo, stehen die Prompts daneben (`.fabrik/develop.prompt.md`).
`projects/fabrik.fabrik.yaml` ist selbst eine Instanz (Repo-Name nur in `vars`).

**Migration privater Specs:** `kyudo-neandertal`, `gods-last-garden` und `ai-lab-merchant-assistant` (samt
`prompts/kyudo-*`, `prompts/ailab-*`) liegen nicht mehr im Repo. Aus der Historie holen und nach `FABRIK_DATA`
(oder ins Zielrepo als `.fabrik/fabrik.yaml`) legen:

```bash
mkdir -p "$FABRIK_DATA/projects/prompts"
for f in kyudo-neandertal gods-last-garden ai-lab-merchant-assistant; do
  git show 5ab9351:fabrik/projects/$f.fabrik.yaml > "$FABRIK_DATA/projects/$f.fabrik.yaml"
done
for f in ailab-address ailab-develop ailab-pr-reply ailab-review kyudo-develop; do
  git show 5ab9351:fabrik/prompts/$f.prompt.md > "$FABRIK_DATA/projects/prompts/$f.prompt.md"
done
fabrik validate
```

### Step-Typen

| Typ      | Felder                                                        | Verhalten |
|----------|---------------------------------------------------------------|-----------|
| `shell`  | `cmd`, `workdir`, `env`, `timeout` (Default 600 s), `name`, `failure_kind` | `sh -c` ausführen; Exit ≠ 0 bricht den Loop ab. `failure_kind` (`outcome`\|`transient`\|`environment`) benennt die [Fehlerklasse](#fehlerklassen-adr-0035) für Exit ≠ 0, z. B. `outcome` für Test-/Lint-Steps. Vorrang haben feste Exit-Codes: 126/127 und 78 (`EX_CONFIG`) → `environment`, 75 (`EX_TEMPFAIL`) → `transient` — so meldet ein Skript die Klasse selbst; Tod durch Signal (OOM-Kill, SIGTERM; Exit > 128) bleibt `unknown`. Nur an shell-Steps erlaubt |
| `git`    | `fetch`, `branch`, `track`, `commit: {add, message}`, `push` | Git-Op; `push` blockt auf geschützten Branches; `commit` auf cleanem Tree ist ein No-Op |
| `pr`     | `repo: owner/name`, `base`, `head`, `title`, `body`          | PR via GitHub API (Token aus `GITHUB_TOKEN` / `FABRIK_ENV_FILE` / `/root/.hermes/.env`). Offener PR für `head` → wiederverwendet; `head` ohne Commits vor `base` → kein PR, Run endet erfolgreich, Folge-Steps `skipped` (Tier-Gate gilt weiter) |
| `agent`  | `backend` (z. B. `opencode`), `prompt`, `workdir`, `endpoint`, `model`, `variant` (nur `opencode`: Modell-Variante aus der opencode-Config, z. B. Reasoning-Aufwand), `permissions`, `timeout`, `github_identity` — oder Legacy: `cmd`, `prompt` | Agent-Task via pluggablem Backend (Session + Prompt + Polling) oder Agent-CLI. Ohne `backend`/`cmd` = sauber übersprungen. `github_identity: dev\|reviewer` (nur CLI-Backends) setzt `GH_TOKEN`/`GITHUB_TOKEN` der Rolle im Agent-Prozess — `gh` (und `git` mit `gh` als Credential-Helper) läuft dann als Builder- bzw. QA-App statt als lokaler User. Ein `gh`-Wrapper vorn im PATH liest das Token pro Call frisch aus `FABRIK_ENV_FILE`, daher überleben Runs mit `timeout` > 1h das 1h-Installation-Token |
| `report` | `text`, `write` (optional: Dateipfad, relativ zum Projekt-Root) | Meldung in das Run-Log und/oder Report-Datei |
| `http`   | `url`, `method`, `headers`, `body`, `expect: {status}`, `emit: {var: body\|status}` | HTTP-Request; Response/Status wird als Variable für folgende Steps verfügbar |
| `llm`    | `prompt`, `schema: {var: typ}`, `untrusted: [vars]`, `provider`, `model`, `temperature`, `extra_context` | Strukturierter LLM-Call; JSON wird gegen das Schema validiert, Keys werden Variablen. Schema-Wert: Typ oder `{type, enum, default}` — `enum` = Whitelist, `default` macht den Key optional. `provider` pinnt einen konfigurierten LLM-Provider (sonst der aktive), `model` überschreibt das Provider-Modell. `untrusted` markiert externe Inhalte als Daten (Prompt-Injection-Schutz). Pro Call wird `usage` (Tokens + Model + Provider) in den Run-Record geschrieben (Tracing-Baustein für Kosten-Exporter) |
| `notify` | `message`, `slack`, `webhooks`                                | Benachrichtigung als Step; ohne Kanäle = nur Log |
| `state`  | `action` (`get`\|`set`\|`add`), `key`, `value` (Substitution erlaubt), `file` (Default `state/<projekt>.json`) | Persistenter State über Runs hinweg (JSON, atomares Write). Emitet `state_value` als Variable. Für "bereits verarbeitet"-Zähler, Round-Caps, Last-Seen in Cron-Loops |
| `alert_route` | `routes: {kategorie: loop}`, `fallback` | Deterministisches Routing `{category}` → Hotfix-Loop (kein LLM); ungemappt → `fallback` bzw. kein Hotfix. Ziele (`none` oder anderer Loop des Projekts) und Route-Zyklen werden beim Spec-Load geprüft. Siehe [Incident-Roundtrip](#incident-roundtrip) |
| `verify` | `checks: [{type, ...}]` (jeder Check ein normales Step-Def: `shell`\|`http`\|…) | Check-Liste-Semantik: alle Checks werden durchlaufen, pro Check Output in den Run-Record. Alle grün → `pass`; einer rot → `fail` mit Check-Name + Output. **Achtung:** Sub-Checks haben volle Step-Semantik — `llm`-Checks mutieren das ctx, `state`-Checks persistieren. LLM-Judges / Screenshot-Diffs können später als Check-Typen dazukommen |
| `issue`  | `action` (`create`\|`edit`\|`label`\|`close`\|`list`\|`block`), `repo`, `number` (außer `create`; Default `{issue_number}`), `title`, `body`, `labels`, `remove_labels`, `state`, `comment`, `append`, `unless_contains`, `only_if`, `skip_if_empty` | Native GitHub-Issue-Operation (REST, kein Shell/`gh`, kein LLM); siehe [`issue`-Step](#issue-step) |
| `review` | `model` (Pflicht), `backend`/`endpoint` (wie `agent`), `target` (optional), `prompt_file` (optional; Auflösung wie bei `agent`) | Cross-Model Cold Critic (ADR 0003): Agent-Session mit **anderem Modell** als der Builder-Step (Engine-Check). Reviewer antwortet mit JSON-Verdict (`approved`\|`changes_requested` + Findings, optional `acceptance` — AC-Verifikation `[{criterion, status: met\|unmet\|unverifiable, evidence}]`, ADR 0013; ungültig → kein Verdict) → landet im Run-Record (`step.review`). `changes_requested` → Run `fail`. `target` Default: Output des letzten Agent-Steps |
| `pr_review` | `repo`, `pr`, `model` (Pflicht), `backend`, `max_rounds` (Default 2), `max_findings` (Default 5), `fix_backend`/`fix_model`/`fix_variant` (Default: letzter Agent-Step), `fix_timeout` (Default 1800 s), `fix_checks: [cmd]`, `fix_check_timeout` (Default 900 s) | LLM-Review des PR-Diffs + GitHub-Review; bei `changes_requested` fixt der Builder (Backend/Modell/Variante des letzten `agent`-Steps) und pusht. `fix_checks` (z. B. Lint + Test) laufen nach dem Fix **vor** Commit/Push; ein roter Check bricht den Step ab (`outcome`), nichts wird gepusht |
| `pr_merge` | `repo`, `pr`, `method` (`squash`\|`merge`\|`rebase`), `update_branch` (Default `false`) | PR mergen. `update_branch: true`: liegt der PR hinter der Base, zuerst `update-branch` (Merge der Base in den PR) und auf Mergebarkeit warten; Konflikt = Step rot (`outcome`). Der Update-Merge-Commit wird **nicht** erneut getestet (Trade-off: sonst bliebe der PR hinter einer wandernden `main` stranden) |

### `issue`-Step

Deterministische Issue-Operationen über die GitHub-REST-API (ADR 0018). Alle
String-Felder sind `{vars}`-substituiert und gehen als JSON-Payload an die API —
nie durch eine Shell, daher ist untrusted Text (LLM-Output, Issue-Body) sicher.

| Feld | Gilt für | Bedeutung |
|------|----------|-----------|
| `action` | alle | `create` \| `edit` \| `label` \| `close` \| `list` \| `block` |
| `repo` | alle | `owner/name` (Pflicht) |
| `number` | `edit`, `label`, `close`, `block` (`list`: wird ausgelassen) | Issue-Nummer; Default: Variable `issue_number` |
| `title` | `create` (Pflicht), `edit` | Titel |
| `body` | `create`, `edit` | Body; bei `edit` + `append: true` an den Bestand angehängt |
| `labels` | `create`, `label` | Labels hinzufügen (Liste oder `"a,b"`); unbekannte legt GitHub an |
| `remove_labels` | `label` | Labels entfernen (fehlendes Label ist kein Fehler) |
| `state` | `edit`, `list` | `open` \| `closed` (`list`: welche Issues, Default `open`) |
| `comment` | `close` | Begründung, vor dem Schließen als Kommentar gepostet |
| `state_reason` | `close` | `completed` \| `not_planned` \| `duplicate`; leer = GitHub-Default `completed` |
| `duplicate_of` | `close` | `#N`: als Duplikat von #N schließen; Issues, die das Duplikat blockiert, werden stattdessen von #N blockiert (ADR 0031) |
| `emit`, `inherit_labels` | `create` | Zielvariable der neuen Nummer (Default `issue_number`); Labels aus der Liste, die Issue `number` trägt, übernehmen (z. B. Priorität) |
| `append`, `unless_contains` | `edit` | Body anhängen; No-Op, wenn der Marker schon im Body steht (Idempotenz) |
| `only_if` | alle | `{var: wert\|[werte]}` — No-Op, wenn nicht jede Variable einen der Werte hat |
| `skip_if_empty` | alle | `[vars]` — No-Op, wenn alle genannten Variablen leer sind |
| `labels`, `limit`, `emit` | `list` | Filter, Obergrenze (Default 100, zuletzt aktualisierte zuerst), Zielvariable (Default `issues`): eine Zeile `#N [Labels] Titel — Body-Auszug` pro Issue (ADR 0031) |
| `blocked_by` | `block` | `"#12, #14"` → native GitHub-"blocked by"-Dependencies (ADR 0031); fehlende/geschlossene Issues und PRs werden übersprungen, max. 5; setzt `blockers` |

`create` setzt `issue_number` und `issue_url` für Folge-Steps.

```yaml
- {type: issue, action: create, repo: o/r, title: "refactor: {t}", body: "{b}", labels: [refactor]}
- {type: issue, action: label, repo: o/r, number: "{issue_number}", labels: [ready], remove_labels: [needs approval]}
- {type: issue, action: close, repo: o/r, number: "{issue_number}", comment: "Duplikat von #65"}
```

**Auth:** `GITHUB_TOKEN` (Env oder `FABRIK_ENV_FILE`, Developer-Identity wie `pr`). Das
Token landet in keinem Step-Ergebnis; Logs und Run-Records werden zusätzlich durch das
Secret-Masking (`[REDACTED]`) geschützt. Fehler → Step `fail` mit HTTP-Status.

### `review`-Beispiel

```yaml
# Builder (z. B. opencode mit Modell A) …
- {type: agent, name: build, backend: opencode, endpoint: "…", model: "modell-a", prompt: "…"}
# … dann Cold Critic mit Modell B (muss ≠ A sein, sonst failt der Step)
- {type: review, name: critique, backend: opencode, endpoint: "…", model: "modell-b"}
# … erst danach der PR-Step
```

Der Reviewer sieht nur das Review-Ziel (frischer Kontext, kein Bias).
Unlesbarer Verdict-Output failt den Step — kein Silent-Pass. Gleiches gilt
für ein ungültiges `acceptance`-Feld (ADR 0013). Im `pr_review`-Step gehen
die AC des verlinkten Issues dem Reviewer als untrusted Daten ins Prompt;
mindestens ein `unmet`es Kriterium erzwingt `changes_requested` (bounded
Fix-Runden), `unverifiable` blockiert nicht, ist aber im Review-Text und im
Run-Record nachvollziehbar.

### `extra_context` (alle LLM-Knoten)

Zusätzlicher Prompt-Kontext für `llm`, `agent`, `review`, `pr_review` (Review- und
Fix-Prompt) und `human_gate`. Auf Projekt-Ebene gilt er für alle diese Steps; ein Step
ergänzt ihn (gleicher `name` → Step gewinnt), `extra_context: false` schaltet ihn ab.

```yaml
extra_context:                       # Projekt-Ebene
  - {name: agents_md, file: AGENTS.md, trusted: true}
loops:
  develop:
    steps:
      - name: review
        type: pr_review
        extra_context:               # Step-Ebene, ergänzt agents_md
          - {name: quality, cmd: "bash scripts/sonar-scan.sh --pr {pr_number}", trusted: true}
          - {name: threads, cmd: "python3 scripts/pr-status.py threads {pr_number} --repo o/r"}
```

Genau eine Quelle pro Eintrag: `file` (relativ zur Step-Workdir — `workdir:` am Step, sonst
die des Runs —, darf sie nicht verlassen), `cmd` (`sh -c` in der Step-Workdir, nur stdout,
`timeout` Default 300 s; Platzhalter nur für vertrauenswürdige Werte wie im `shell`-Step)
oder `value` (Template). **Default ist untrusted**: der Inhalt wird wie bei `untrusted:` als
Daten markiert. `trusted: true` nur für repo-eigene Dateien und Tool-Ausgaben — Issue-Text
oder PR-Kommentare bleiben untrusted. `<untrusted`/`<context`-Tags im Inhalt werden
neutralisiert (`&lt;`), damit Text keinen Block schließen oder vortäuschen kann.
Quellen werden bei jedem Prompt-Aufbau neu geladen (`pr_review`: pro Runde). Eine nicht
ladbare Quelle bricht den Step nicht ab; der Block nennt den Grund, das Log warnt.
Budget: pro Quelle `max_chars` (Default 50 000 Zeichen), alle Quellen eines Prompts
zusammen höchstens 120 000 Zeichen — in Spec-Reihenfolge, was nicht passt, wird gekürzt bzw.
ausgelassen (Log-Warnung). Checks in einem `verify`-Step erhalten den Projekt-Kontext wie
Top-Level-Steps. Die Loop-Templates `develop` und `review-pr` reichen ihn über die Parameter
`builder_context`/`review_context` an ihre LLM-Steps durch.

### `sonar-scan` PR-Modus (Quality-Report im Review, advisory)

`bash scripts/sonar-scan.sh` analysiert `main` und prüft das Quality Gate (Loop `sonar-scan`,
täglich). Mit `--pr <n> [--post] [--base main]` scannt es stattdessen die aktuelle Workdir
(PR-Head) unter eigenem Key `<projectKey>:pr-<n>` — die Community Edition kann keine
PR-Analyse, die main-Analyse bleibt so unberührt. Ein `coverage.xml`, das jünger als der
HEAD-Commit ist, wird wiederverwendet (der develop-`test`-Step erzeugt es). Statt des Gates
schreibt `scripts/sonar-pr-report.py` einen deterministischen Markdown-Report nach stdout:
Sonar-Issues nur auf den geänderten Zeilen, Lint (ruff check + ruff format + mypy) auf den
geänderten Zeilen, Coverage der geänderten Zeilen (`diff-cover`), Kennzahlen; letzte Zeile
`<!-- fabrik-quality sha=<head> -->`. `--post` legt ihn als einen
PR-Kommentar an bzw. aktualisiert ihn. Als `extra_context`-Quelle (`trusted: true`) landet er
in jedem `pr_review`-Prompt; `review-pr` liest den Kommentar. Logs gehen nach stderr.

### `state`-Beispiele

```yaml
# Letzten verarbeiteten PR-Stand merken (Cron-Loop, idempotent)
- {type: state, name: last, action: get, key: last_pr_head}
- {type: state, name: mark, action: set, key: last_pr_head, value: "{pr_head_sha}"}

# Round-Cap: Counter hochzählen, Loop bricht, wenn >= 5
- {type: state, name: bump, action: add, key: rounds, value: "1"}

# Liste pflegen (nicht-numerische value → append)
- {type: state, name: seen, action: add, key: seen_issues, value: "{issue_number}"}
```

Der State lebt unter `state/<projekt>.json` im Projekt-Root (Default) —
bewusst **deterministisch** (kein Agent-Zugriff), damit Cron-Loops
wiederholte Runs sauber erkennen. Path-Traversal außerhalb der Projekt-Root
wird blockiert.

**`gate`** (pro Step, Default `true`): Steuert, was ein Step-Fehler tut.
`gate: true` → der Loop bricht sofort ab (Run = `fail`). `gate: false` → der
Fehler wird notiert, der Loop läuft weiter, der Run endet am Ende als `fail`.
Nützlich für "best-effort"-Steps (z. B. optionale Cleanup- oder
Benachrichtigungs-Steps), die den Rest des Loops nicht blockieren sollen.

Jeder Step kann `approval: true` haben → der Run pausiert **vor** diesem Step
mit Status `needs_approval`. Freigabe: `fabrik approve <lauf_id>`,
`POST /api/approve` oder der Button in der UI. Ablehnung bricht den Run ab.
Dadurch bleibt der Mensch komplett aus dem Loop heraus — und greift nur bei
den kritischen Aktionen zu.

Bedingtes Gate (ADR 0017): `approval_when: {label: [needs approval, close]}`
pausiert nur, wenn die Run-Variablen passen (AND über Variablen, OR über
Werte; fehlende Variable → kein Gate). Nicht mit `approval: true`
kombinierbar. Ein unerfülltes `approval_when` ersetzt das Tier-Gate nicht.

Begründung der Freigabe: `approval_reason: "{reasoning}"` (Template mit
Run-Variablen, z. B. aus einem vorherigen `llm`-Step) erscheint im
Freigabe-Kontext (UI, Chat, Log) als **warum** — neben **was** (aus der Spec
abgeleitete Aktion) und der erfüllten **bedingung**. Fehlt eine referenzierte
Variable, entfällt die Zeile.

**Risk-Tiers** (pro Loop: `tier: 1-4`): T1 läuft nach grünen Checks automatisch
durch; T2+ endet mit einem synthetischen Tier-Approval-Step (`needs_approval`),
auch wenn kein einzelner Step `approval: true` hat. Das Mapping steckt in einer
Funktion (`tier_policy`) — P1/P2 erweitern es, statt Logik zu streuen.

**Locking** (ADR 0020): Alle Loops eines Projekts teilen einen Git-Worktree und
laufen daher nacheinander (Projekt-Lock). Ein Loop mit `worktree: false` bekommt
stattdessen eine leere Workdir pro Run und einen eigenen Lock — er läuft parallel
zu allen anderen Loops, aber nie doppelt. Erlaubt nur ohne `git`/`pr`/`agent`/
`review`/`pr_review`-Steps (Spec-Validierung). Ändert sich die Spec zwischen
Lock-Wahl und Run, fällt der Run auf den Projekt-Lock zurück; Scratch-Cleanup-
Fehler landen im Run-Log.

Substitution in `cmd`/`env`/`text`/`prompt`: `{project}`, `{root}`,
`{workdir}`, `{branch}`, `{head}`, `{loop}`, `{trigger}`, `{run_id}`,
`{fabrik_home}` (fabrik-Checkout, z. B. für `{fabrik_home}/scripts/pr-status.py` aus
fremden Projekten) + alle `env:`-Variablen der Spec.

### Agent-Backends (P1)

`agent`-Steps dispatchen über ein pluggables Backend-Interface (SPIE) —
der Conductor bleibt deterministisch, der Agent ist austauschbar:

| Backend    | Setup                                                        |
|------------|--------------------------------------------------------------|
| `opencode` | `opencode serve --port 8945` + `fabrik.env`: `FABRIK_AGENT_ENDPOINT`, `FABRIK_AGENT_MODEL` (Format `provider/model`); 2.x zusätzlich `FABRIK_AGENT_PASSWORD` |
| (Legacy)   | `cmd:` mit jeder Agent-CLI (z. B. `claude -p {prompt_file}`) |

Sicherheitsschichten für Agent-Prozesse (einzeln zuschaltbar, siehe ADRs):

- **Agent-Env-Allowlist (ADR 0050):** Agent-Subprozesse erben kein `os.environ` —
  nur die Allowlist (`agentenv.py`, erweiterbar per `FABRIK_AGENT_ENV_EXTRA`) plus
  pro Step explizit übergebene Variablen. Für den geteilten `opencode serve`-Prozess:
  `fabrik agent-env` druckt die gescrubte Env (systemd-`EnvironmentFile` / Docker
  `--env-file`).
- **Egress default-deny (ADR 0051):** Agent-Container hängen an `fabrik-sandbox`
  (kein direkter Internetzugang); der gesamte Egress läuft durch einen Tinyproxy mit
  Domänen-Allowlist (`fabrik egress start`; Allowlist: Builtins + optional `FABRIK_EGRESS_ALLOWLIST_FILE`/
  `FABRIK_EGRESS_ALLOWLIST`, validiert). Denied-Requests sind im Proxy-Log ein Injection-Signal.
  Bekannte Grenze: der geteilte `opencode serve`-Prozess läuft ungesandboxt auf dem Host mit
  vollem Netz und wird vom Proxy nicht erfasst (nur Agenten in `fabrik-sandbox`).
- **Per-Run-Agent-Sandbox (ADR 0054, in Überarbeitung):** Mit `FABRIK_SANDBOX=1` läuft jeder
  One-Shot-CLI-Agent-Run in einem gehärteten Docker-Container (`--cap-drop ALL`,
  no-new-privileges, Resource-Caps, read-only root fs, Egress nur über den ADR-0051-Proxy).
  Kein Mount des Worktrees: Der Stand geht als git-Bundle hinein, das Ergebnis als Bundle
  zurück; fabrik prüft es und übernimmt den Diff in den Worktree. Das Image braucht die
  Agent-CLI und git; Vorlage mit claude-code, codex, copilot, gh, Python/uv:
  `docker build -t fabrik/agent-sandbox:latest deploy/agent-sandbox` (Versionen per
  `--build-arg`, Basis-Images per Digest gepinnt; fabrik startet es mit `--pull never`).

Das `opencode`-Backend (Details: `docs/spike-opencode.md`, 1.x/2.x-Dual-Support:
ADR 0005):
- Unterstützt opencode serve **1.x und 2.x** — die Server-Version wird
  automatisch erkannt (`/api/info` vs. `/api/health`). 2.x verlangt
  HTTP-Basic-Auth: `OPENCODE_SERVER_PASSWORD` am Server +
  `FABRIK_AGENT_PASSWORD` in `fabrik.env` (gleicher Wert)
- Session pro Step (Workdir über `?directory=` (1.x) bzw. `location`
  (2.x) → Arbeit im Step-Workdir, ein serve-Prozess bedient viele Projekte)
- Prompt + Polling der Message-API (kein SSE nötig);
  Done-/Failed-Erkennung über `finish` + Substanz-Check (erledigtes Tool oder
  Final-Text) — leere/falsche Messages (z. B. LLM-500) → Step `fail`, kein
  30-Minuten-Hang
- Token-/Cost-Accounting pro Run (aus der Message-API)
- Health-Gate vor Dispatch; Timeout → `abort`/`interrupt`; Exception
  mid-Run bricht die Session ab (kein Orphaning)
- Robust gegen transiente Backend-/Transport-Fehler (ADR 0012): wirft
  `poll()`/`transcript()` (z. B. `opencode serve` down/Restart), pollt der
  Step mit Backoff weiter — **innerhalb des Step-Timeouts** — statt den Run
  zu killen. Anhaltender Fehler bis zum Timeout → `fail` mit Backend-Ursache
- Containment: File-Tools (`edit`) sind auf die Session-Workdir gefangen
  (`external_directory` = ask → in headless deny); `bash "*` ist bewusst weit —
  echte Isolation (non-root User, Container, VM) ist eine **Deployment-
  Voraussetzung**, keine Config-Option. Override: `step.permissions`
- Security: Agent-Steps sind T2+ — der Conductor vertraut nur dem
  deterministischen Step-Outcome, nicht Agent-Prosa; Untrusted-Inputs
  (Issue-Bodies) sind Daten im Prompt

### Triggers

- **manual** — UI-Button, CLI, API, Chat
- **cron** — 5 Felder (`min stag dom mon dow`, `*`, `*/n`, `a-b`, Listen).
  `on_start: true` feuert zusätzlich einmal nach dem Serverstart (bzw. beim ersten
  Tick nach einem Resume) — z. B. für `token-refresh`, damit nach einer Server-Pause
  kein Loop mit einem abgelaufenen 1h-Token startet. Bis dieser Run fertig ist (max.
  2 min), warten die übrigen Trigger des Projekts (Cron, Datei, Issue- und PR-Polls)
- **on_file** — feuert, wenn eine Datei im Glob (relativ zu `root`) sich
  ändert und `debounce` Sekunden stabil ist
- **on_issue** — pollt GitHub (`repo` aus Spec oder Trigger, optional
  `labels`), startet den Loop bei jeder neuen Issue. Mit `reconcile: true`
  (ADR 0014) feuert er stattdessen für jedes offene Issue, für das der Loop
  noch nie einen Run hatte — überlebt Restarts; max. `max_per_poll`
  (Default 2) Runs pro Poll, älteste zuerst. Optional `priority_labels`
  (ADR 0021, z. B. `[p0, p1, p2, p3]`): Issues mit höherem Prioritäts-Label
  zuerst, unlabelte zuletzt. `skip_blocked: true` (ADR 0031, nur mit
  `reconcile`): Issues mit unerledigten GitHub-"blocked by"-Dependencies starten
  nicht, sondern beim ersten Poll, nachdem der letzte Blocker als `completed`
  geschlossen wurde (`duplicate`/`not_planned` blockieren weiter)
- **on_pr** — pollt GitHub-PRs (`repo` aus Spec oder Trigger, optional
  `labels` (AND-Semantik), `poll` in Sekunden, `states: [open|closed]`
  (Default `[open]`)), startet den Loop bei neuen/aktualisierten PRs
  (updated_at-Change) und bei State-Wechseln (z. B. open→closed, wenn
  `closed` in `states` steht). Dedup: eine PR feuert nur bei Change —
  erneutes Pollen derselben PR löst keinen zweiten Run aus. Variablen:
  `{pr_number}`, `{pr_title}`, `{pr_head_ref}`, `{pr_body}`.
  `reconcile: true` (ADR 0011): feuert auch auf bestehende PRs, aber nur
  wenn noch kein vorheriger `on_pr`-Run existiert (Status egal; restart-fest)
  (Reconciling-Controller). `reconcile: head` (ADR 0023): wie `true`, aber
  pro Head-SHA (`on_pr:#N@<sha>`) — jeder neue Push feuert genau einmal, ein
  verworfener Trigger (Lock belegt, Restart) wird beim nächsten Poll
  nachgeholt. Zusätzliche Variable: `{pr_head_sha}`.

### Pause / Disable (ADR 0019)

Zustand pro Projekt und Loop: `active` | `paused` (Trigger feuern nicht,
manuelle Runs erlaubt) | `disabled` (zusätzlich keine manuellen Runs).
Projekt-Zustand schlägt Loop-Zustand; laufende Runs laufen zu Ende. Optional
`until` (ISO 8601, ohne Zeitzone = UTC) → automatisches Resume. Persistiert in
`state/run-states.json` (nicht in der Spec); jede Änderung steht im Audit-Log
`state/run-state-changes.jsonl` (wer, wann, von, zu, until).

```bash
fabrik pause <projekt> [loop] [--until 2026-01-31T18:00]
fabrik disable <projekt> [loop]
fabrik resume <projekt> [loop]
```

Chat: `pause|resume|disable <projekt> [loop]`; UI: Buttons pro Projekt/Loop;
API: `POST /api/projects/<p>/state`. Der Not-Aus für alle Projekte ist nicht
Teil von v1. Zugriff: Rolle `operator` (siehe „Authentifizierung und Rollen“).

- **on_webhook** — `POST /api/webhook/<projekt>/<loop>` (ADR 0027). Auth per
  HMAC-SHA256 des Raw-Bodys im Header `X-Fabrik-Signature: sha256=<hex>`; falsch/
  fehlend → `401`. Payload→Variablen per `map` (Punkt-Pfade; ungültiges JSON `400`,
  fehlendes Feld `422`). CORS nur für `FABRIK_WEBHOOK_ORIGIN` (Tunnel-Origin, nie
  `*`); über diesen Host ist nur die Webhook-Route erreichbar.
  ```yaml
  triggers:
    - type: on_webhook
      secret_env: MYAPP_HOOK_SECRET   # Env-Name, nie der Wert
      map: {issue_number: issue.number, title: issue.title}
  ```

- **on_alert** — `POST /api/alert/<projekt>/<loop>` (ADR 0045): Sentry-/CloudWatch-Intake.
  Auth wie `on_webhook` (HMAC im `X-Fabrik-Signature`; Sentry zusätzlich
  `Sentry-Hook-Signature`), sonst `401`. Variablen: `alert_source|title|message|level|service|url|fingerprint`.
  CloudWatch-Zustand ≠ `ALARM` → `200 ignored`, kein Run. Über den Tunnel-Host erreichbar.

- **on_slack** — pollt Slack-Channels (ADR 0040) und startet pro neuer, zur
  Signatur passender Nachricht genau einen Run (Dedup per `ts` in
  `state/slack/`). Variablen: `{alert_text}`, `{alert_channel}`, `{alert_ts}`.
  `alert_text` ist untrusted (jeder Slack-Nutzer im Channel kann ihn setzen):
  in `llm`-Steps nur über `untrusted: [alert_text]`, in `shell`-Steps nur über
  `env:` — nie inline in `cmd` (ADR 0008). Token nur per Env-Variable
  (`token_env`), nie im Spec; nur Channels der `channels`-Whitelist werden gelesen.
  ```yaml
  triggers:
    - type: on_slack
      token_env: SLACK_BOT_TOKEN      # Env-Name, nie der Wert
      channels: [C0123ALERTS]         # Whitelist (Channel-/DM-IDs)
      pattern: "^(ALERT|FIRING)"      # optional, Regex
      poll: 60
      max_pages: 20                   # optional, History-Seiten (à 100) pro Channel und Poll
      max_alert_chars: 4000           # optional, längerer alert_text → "… [gekürzt, N Zeichen]"
  ```
  Ist `max_pages` erreicht, verarbeitet der Poll die neuesten Nachrichten und holt
  die ältere Lücke in den Folge-Polls nach (Warnung im Log) — es geht kein Alert
  verloren, nur die Reihenfolge über Polls hinweg ist dann nicht chronologisch.
  `pattern` prüft immer den vollen Text, gekürzt wird erst danach.

| Trigger | Quelle | Mechanismus | Auth |
|---|---|---|---|
| `on_issue` | GitHub-Issues | Polling | GitHub-Token |
| `on_webhook` | HTTP-Sender | Push | HMAC-SHA256 |
| `on_slack` | Slack-Channels/DMs | Polling | Bot-Token (Env) |

### Relay-Tasks (ADR 0037)

Für Aufgaben, die nicht in einen Run passen (historischer Import, Migration, Analyse über Tage).
Ein Issue mit Label `fabrik-relay` ist der Auftrag. Es läuft in vielen kurzen Sessions, die
Handoff-Datei liegt auf dem Branch `fabrik/relay/<N>`. Bei `done` prüft ein unabhängiges,
zunächst blindes Audit das Ergebnis. Freischalten pro Projekt:

```yaml
relay:
  allowed: [alice]             # Pflicht: nur diese Logins dürfen Briefs freigeben (Label + Body)
  forbid: [infrastructure/**]  # Pfade, die Sessions nie committen
  builder: {model: …}          # Sessions (github_identity dev)
  reviewer: {model: …}         # Audit (github_identity reviewer, read-only)
  bot_login: my-builder-app    # Pflicht: Status-Kommentar nur von dieser Identität lesen; Commit-Autor
  retry: {max: 3, backoff: [5m, 30m, 2h], "on": [interrupted, transient], max_wait: 24h}  # wie Run-Retry (ADR 0036); Default aus
  trail_archive: {type: git-ref}  # nach Audit-pass Trail archivieren + vom Branch entfernen (git-ref | local)
  # optional: repo, base, label, dispatch (cron), max_session_minutes, verify_timeout,
  #           audit_timeout, audit: false
```

Daraus erzeugt fabrik die Loops `relay-dispatch` (cron, kein Agent), `relay-session` und
`relay-audit`. Brief-Vorlage und Ablauf: [docs/relay.md](docs/relay.md). Ein Mensch entblockt
(Checkpoint, Stillstand, geänderte `## Verify`), indem er das Label `relay: blocked` entfernt.
`FABRIK_URL` (Default `http://127.0.0.1:8321`) und ggf. `FABRIK_TOKEN` braucht der Dispatcher
für `POST /api/runs`.

**Trail-Archiv** (ADR 0037, Nachtrag 2026-11-03): Mit `trail_archive` legt `audit-publish` nach
`pass` den Trail (`.fabrik/relay/<N>/`) unveränderlich ab (`git-ref`: versteckter Ref
`refs/fabrik/relay/<N>/audit-<k>`; `local`: `FABRIK_DATA/relay-archive/`), prüft das Archiv gegen
den Trail-Hash und entfernt den Trail erst dann per Lösch-Commit vom Branch. PR-Diff und Merge
enthalten so nur die eigentliche Änderung. Das Verdikt ist an den Hash gebunden (letzte Zeile von
`AUDIT.md`, Status-Kommentar). Braucht eine spätere Session oder ein erneutes Audit den Trail, wird
er aus dem Archiv wiederhergestellt (Hash-Prüfung, sonst scheitert der Step). Ohne `trail_archive`
bleibt der Trail auf dem Branch; der Status-Kommentar warnt, sobald ein PR vom Branch existiert.
### Incident-Roundtrip

Alert → Triage → Hotfix → Verify → PR (ADR 0045). Abgrenzung: weitere Intake-Quellen sind
#33 (`on_slack`) und #34 (`on_datadog`); der ACP-Backend-Status steht in ADR 0044 (beobachten).

```yaml
loops:
  triage:
    worktree: false
    triggers:
      - {type: on_alert, source: sentry, secret_env: SENTRY_HOOK_SECRET}  # oder cloudwatch
    steps:
      - name: triage            # read-only: nur Klassifikation, Alert-Felder untrusted
        type: llm
        prompt: "Klassifiziere den Alert {alert_title}: {alert_message}"
        untrusted: [alert_title, alert_message]
        schema:
          category: {type: string, enum: [bug, infra, noise, unknown], default: unknown}
      - {name: route, type: alert_route, routes: {bug: hotfix}}   # unbekannt/noise → kein Hotfix
  hotfix:                       # läuft als eigener Run mit alert_* + category
    steps: [agent-Fix, verify, pr]
```

Quellen: **Sentry** — Internal Integration/Webhook auf die URL, Client Secret als
`secret_env`. **CloudWatch** — Alarm → SNS → Relay (Lambda/API Gateway), das den Body mit
HMAC (`X-Fabrik-Signature: sha256=<hex>`) signiert und weiterreicht; SNS-Envelope wird
entpackt. Der Hotfix-Run unterliegt den normalen Tier-/Approval-Gates.

### Alerts (`notify`)

Nach jedem Run, dessen Status in `events` steht:
- **slack** — Slack-Webhook-URL (string oder `{url: …}`)
- **webhooks** — beliebige HTTP-POST-Ziele (Datadog HTTP-Intake, CloudWatch,
  eigene Endpunkte); Payload: `projekt, loop, status, lauf, trigger, message, log_tail`,
  bei `run_failed` zusätzlich `failure_kind` (auch im Text und Issue-Titel; `environment`
  hervorgehoben, ADR 0035)
- **github_issue** — bei `run_failed` ein Issue im `github`-Repo anlegen
  (Titel + Log-Tail + Label). **Dedup:** gleiches Fehlerbild (Projekt/Loop +
  fehlgeschlagener Step + Fehlertext ohne Run-/Session-IDs, d. h. UUIDs und
  Hex-Tokens mit a-f; dezimale Zahlen — auch lange wie Timestamps — bleiben
  erhalten) → solange das Issue offen ist, wird es kommentiert statt ein
  neues anzulegen. Der Lookup paginiert über alle offenen Issues mit den
  `issue_labels` (UND-verknüpft — nach einer Label-Änderung matchen alte
  Issues nicht mehr). Tuning-Konstanten in `config.py`:
  `ALERT_OPEN_ISSUES_PAGE` (max. 100), `ALERT_LOOKUP_MAX_PAGES`,
  `ALERT_FP_LEN`, `ALERT_REASON_CAP` (Details: ADR 0025)
- **log_tail-Policy:** bekannte Secrets (Env/Token-Dateien) sind im Log
  bereits maskiert; zusätzlich redigiert `secrets.redact_text` Bearer-/API-/AWS-Keys,
  Private-Key-Blöcke und `key=`/`token=`-Assignments zu `[REDACTED]` (Schalter
  `config.ALERT_LOG_REDACTION`, Default an; bei Redaction-Fehler Fail-safe:
  unredigiert + Warning). Heuristik — Ziele trotzdem eingeschränkt halten
  (ADR 0025, ADR 0026)

Testen: `fabrik alert test [--projekt X]` oder `alert test` im Chat.

## Chat (UI + CLI + API)

Das Chat-Interface baut, ändert und debuggt Loops — per Befehl oder per
LLM-Copilot.

**Befehle:** `help`, `list`, `show <projekt>`, `lauf <projekt> <loop> [notiz]`,
`runs [projekt]`, `log <lauf_id>`, `approve <lauf_id>`, `reject <lauf_id>`,
`alert test [projekt]`, `apply <projekt>`

**LLM-Copilot:** alles, was kein Befehl ist, geht an den **aktiven**
LLM-Provider (OpenAI-kompatibel). Provider werden in `fabrik.env` konfiguriert
und in der UI umgeschaltet (Select oben rechts, `POST /api/llm`):

- **Named-Provider** (beliebig viele, z. B. Ollama + lokale llama.cpp):
  `FABRIK_LLM_PROVIDERS=ollama,llama-cpp` +
  `FABRIK_LLM_OLLAMA_URL`/`_MODEL`/`_KEY` und
  `FABRIK_LLM_LLAMA_CPP_URL`/`_MODEL`/`_KEY`
  (Bindestriche im Namen → Unterstriche im Env-Präfix).
- **CLI-Provider** (`_CLI` statt `_URL`, Auth = Login der CLI, kein Key):
  `FABRIK_LLM_CLAUDE_CLI=claude` / `FABRIK_LLM_COPILOT_CLI=copilot` + `_MODEL`.
  Läuft ohne Tools und ohne Repo-Instruktionen (reine Text-Antwort);
  `temperature`/`max_tokens` werden ignoriert.
- **Legacy** (ein Endpunkt, wird Provider `default`): `FABRIK_LLM_URL`,
  `FABRIK_LLM_MODEL`, optional `FABRIK_LLM_KEY`.

Der aktive Provider wird in `state/active_llm` persistiert (überlebt
Neustarts); Default ist der erste konfigurierte Provider. `llm`-Steps können
per `provider:` einen Provider pinnen und per `model:` das Modell überschreiben.
Der LLM sieht die aktuellen Specs + die letzten Fehler-Runs; schlägt er eine
Spec-Änderung vor, wird sie validiert und wartet auf `apply <projekt>` (nichts
wird ohne Zustimmung geschrieben). Während ein LLM-Call läuft, zeigt der Chat
animierte Punkte + einen **✕ abbrechen**-Button (`POST /api/chat/cancel`); der
Request wird sofort beendet; ein HTTP-Call läuft im Hintergrund zu Ende, eine
CLI wird gekillt. Das Modell-Dropdown sitzt in der Titelleiste des Chats.

## API

| Route | Methode | Beschreibung |
|-------|---------|--------------|
| `/api/projects` | GET | Projekte + Loops + letzter Run + github/notify |
| `/api/schedule` | GET | Schedule-View: alle cron-Loops mit nächsten Fire-Zeiten, letztem Run, State + angesetzt One-time-Overrides; `queue` = tatsächliche Queue (laufend/wartend/Freigaben, Worker-Pool, Slot-Kontext) |
| `/api/schedule/onetime` | POST `{"project","loop","when","note"?}` | One-time-Run ansetzen (`when`: `15:05` · `in 10 min` · ISO-8601); ersetzt einen bestehenden Eintrag pro Loop; feuert der Scheduler (disabled → verworfen, paused → läuft) |
| `/api/schedule/onetime` | DELETE `{"project","loop"}` | angesetzten One-time-Run entfernen |
| `/api/projects/<name>/spec` | GET | volle (expandierte) Spec + `_hash` (Optimistischer Lock) |
| `/api/projects/<name>/spec?raw=1` | GET | rohe, nicht expandierte Spec (vars wörtlich, `use:`/`insert:` un-inlined, alle Loop-Schlüssel) + `_hash` — Draft-Quelle für den UI-Graph-Editor |
| `/api/projects/<name>/spec` | PUT `{"loops"?,"remove_loops"?,"description"?,"max_parallel"?,"base_hash"?}` | Spec-Edit (Graph-View): Loops pro Loop mergen + löschen, Top-Level-Felder setzen; `base_hash` ≠ aktueller Hash → 409. Validierung + atomarer Write, Kommentare erhalten |
| `/api/projects/<name>/branches` | GET | verfügbare Branches + aktueller Branch (ADR 0004) |
| `/api/projects/<name>/branch` | POST `{"branch"}` | Ziel-Branch wechseln (Override, ADR 0004) |
| `/api/projects/<name>/state` | POST `{"state","loop"?,"until"?}` | `active`/`paused`/`disabled` für Projekt (oder Loop) setzen (ADR 0019) |
| `/api/runs?project=&limit=` | GET | Run-Liste (neueste zuerst) |
| `/api/runs?project=&q=&from=&to=&status=&failure_kind=&loop=&cursor=&limit=` | GET | Historie: Freitext (Run-Id, Loop, Trigger, Status, Notiz, Step-Summaries; nicht das Log), Datumsbereich, Status/Fehlerklasse (kommagetrennt)/Loop; Cursor-paginiert → `{runs, next_cursor}` (ADR 0034, 0035) |
| `/api/runs` | POST `{"project","loop","note"}` | Run starten; läuft das Projekt, wird er eingereiht (202, `queued: true`, Record-Status `queued`) und startet mit Vorrang vor Trigger-Runs (ADR 0015). Response enthält `run_id` |
| `/api/run/<id>` | GET | Run-Record (Steps, Output) + `cost`: Token-/Kosten-Summe aus den Step-Usages, aufgeschlüsselt je Step und Backend; `unpriced_steps` > 0 = Backend lieferte keine Kosten (Summe ist Untergrenze) |
| `/api/kpis/costs?project=&from=&to=` | GET | Kosten-Rollup je Projekt (Runs, Tokens, `cost_usd`, je Backend) im Zeitraum (`from`/`to` wie in der Historie, `to` als Datum = ganzer Tag) + Gesamtsumme; die Run-Liste trägt je Zeile `cost` (Issue #126) |
| `/api/run/<id>/log?lines=N` | GET | rohes Run-Log als Plain-Text (Tail via `lines`) |
| `/api/run/<id>/stream?project=` | GET | Live-Log + Statuswechsel als Server-Sent Events (`log`/`status`/`end`); Reconnect via `Last-Event-ID` (Byte-Offset); Secrets redigiert (ADR 0038) |
| `/api/run/<id>/transcript?after=&limit=` | GET | Agent-Turns (Prompt, Tool-Calls, Antworten) aus dem SessionLog, seitenweise (ADR 0038) |
| `/api/chat` | POST `{"message","project","req_id"}` | Chat (Befehl oder LLM); antwortet mit `req_id` |
| `/api/chat/cancel` | POST `{"req_id"}` | laufenden Chat-Request abbrechen (UI-Button) |
| `/api/approve` / `/api/reject` | POST `{"project","run_id"}` | Approval-Gate; Response enthält `resumed` (bool) |
| `/api/llm` | GET | LLM-Provider (Key nie) + aktiver Provider |
| `/api/llm` | POST `{"provider"}` | aktiven LLM-Provider setzen (persistiert) |
| `/api/health` | GET | Liveness |

Alle `POST`-Routes prüfen Origin (eigener Host) und — falls `FABRIK_TOKEN`
gesetzt ist — das Bearer-Token; sonst `403`. Mit `FABRIK_AUTH=local` prüfen
**alle** `/api/`-Routes (auch `GET`) Session oder Token gemäß Rolle (`401`/`403`);
zusätzlich `/api/login` (POST), `/api/logout` (POST), `/api/session` (GET).

### Verbindungen (ADR 0052)

LLM-Provider und Agent-Backends lassen sich als **benannte Verbindungen** in der UI pflegen (⚙ oben rechts,
Schreiben nur als `admin`). Env-Konfiguration (`FABRIK_LLM_*`, `FABRIK_AGENT_*`) gilt weiter und erscheint
schreibgeschützt (`env`); eine UI-Verbindung gleichen Namens überschreibt sie.

- Ein Agent-Step wählt per `connection: <name>`; ohne Angabe greift die als Standard markierte Verbindung
  (wenn ihr Backend zum Step passt). Rangfolge: Step-Felder > `connection:` > Standard > Env.
- Secrets (API-Key, Server-Passwort) liegen Fernet-verschlüsselt in der Tabelle `connections` der
  State-DB (`FABRIK_DB_URL`) und werden nie über die API ausgegeben. Schlüssel: `FABRIK_SECRET_KEY`,
  sonst `state/secret.key` (0600, automatisch erzeugt). Ohne den Schlüssel sind gespeicherte Secrets
  unbrauchbar — Schlüssel getrennt von der DB sichern.

```yaml
steps:
  - {type: agent, name: changes, connection: hetzner-opencode, prompt_file: prompts/develop.md}
```

### Authentifizierung und Rollen (ADR 0030)

`FABRIK_AUTH=none` (Default): wie bisher, nur Loopback-Bind. `FABRIK_AUTH=local`:
Login mit lokalen Benutzern (`state/users.json`, scrypt), Session-Cookie
(HttpOnly, SameSite=Strict, `FABRIK_SESSION_TTL` Sekunden, Default 28800) und
CSRF-Header `X-CSRF-Token` bei schreibenden Requests. Rollen: `viewer` (lesen),
`operator` (+ Runs starten, approve/reject, pausieren, Chat), `admin`
(+ LLM-Provider, Branch-Wechsel, Spec-`apply`). `FABRIK_TOKEN` bleibt als
API-Token (= admin, ohne CSRF) für CLI/Automation. Bind auf eine
Nicht-Loopback-Adresse (`fabrik serve --host 0.0.0.0` / `FABRIK_HOST`) ohne
`local` wird verweigert; TLS per Reverse-Proxy (+ `FABRIK_COOKIE_SECURE=1`).

```bash
export FABRIK_AUTH=local
fabrik user add anna operator        # Passwort wird abgefragt (oder --password-stdin)
fabrik user passwd anna
fabrik user role anna admin
```

Run-Records enthalten `started_by`, `approved_by`, `rejected_by`.

#### OIDC-Login (`FABRIK_AUTH=oidc`, ADR 0046)

Anmeldung über einen zentralen IdP (Authorization Code Flow + PKCE, `state`/`nonce`, ID-Token-Prüfung
per JWKS). Braucht das Extra: `pip install 'fabrik[oidc]'` (`uv sync --extra oidc`); ohne bricht
`fabrik serve` mit einer Meldung ab. Im IdP die Redirect-URI `https://<host>/api/oidc/callback`
registrieren (https Pflicht, `http` nur Loopback) und einen Confidential Client mit Secret anlegen.
Der `state` wird zusätzlich per kurzlebigem Cookie (`fabrik_oidc_state`, `SameSite=Lax`) an den
Browser gebunden, der den Login begonnen hat (Schutz vor Login-CSRF). Abmelden beendet nur die
fabrik-Session, nicht die beim IdP; die UI leitet danach nicht automatisch erneut zum IdP.

| Variable | Bedeutung |
|---|---|
| `FABRIK_OIDC_ISSUER` | Issuer-URL (Discovery: `<issuer>/.well-known/openid-configuration`) |
| `FABRIK_OIDC_CLIENT_ID` / `_CLIENT_SECRET` | Client-Zugangsdaten |
| `FABRIK_OIDC_REDIRECT_URI` | muss beim IdP registriert sein |
| `FABRIK_OIDC_ROLE_MAP` | `gruppe=rolle,…` (Rollen `viewer`/`operator`/`admin`; höchste gewinnt) |
| `FABRIK_OIDC_GROUPS_CLAIM` | Gruppen-Claim (Default `groups`) |
| `FABRIK_OIDC_SCOPES` | Default `openid profile email` |

Wer keiner gemappten Gruppe angehört, wird abgewiesen (403). Beispiele:

```bash
# Keycloak (Gruppen-Mapper "Group Membership" → Claim "groups", Full group path aus)
export FABRIK_OIDC_ISSUER=https://kc.example.com/realms/acme
export FABRIK_OIDC_CLIENT_ID=fabrik FABRIK_OIDC_CLIENT_SECRET=...
export FABRIK_OIDC_ROLE_MAP="fabrik-admins=admin,fabrik-ops=operator"

# AWS Cognito (Issuer = User Pool: https://cognito-idp.<region>.amazonaws.com/<pool-id>)
export FABRIK_OIDC_ISSUER=https://cognito-idp.eu-central-1.amazonaws.com/eu-central-1_AbCdEf
export FABRIK_OIDC_GROUPS_CLAIM=cognito:groups
export FABRIK_OIDC_ROLE_MAP="fabrik-admins=admin,fabrik-ops=operator"

# Entra ID (Gruppen-Claim in der App-Registrierung aktivieren; Gruppen kommen als Objekt-IDs)
export FABRIK_OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
export FABRIK_OIDC_GROUPS_CLAIM=groups
export FABRIK_OIDC_ROLE_MAP="11111111-aaaa-...=admin,22222222-bbbb-...=operator"
```

`POST /api/runs` liefert `run_id` **vor** Worker-Start zurück — die UI
(`web/app.js`) pollt danach per `GET /api/run/<id>` (vermeidet das
Start-Race, bei dem der neue Run noch nicht in der Historie sichtbar war).
`POST /api/approve`/`/api/reject` liefert `resumed` (bool): `true` nur, wenn
für den Run ein Resume-Worker gestartet wurde (z. B. nach Approval-Gate);
`false`, wenn der Run bereits läuft oder abgeschlossen ist. Integratoren
sollten `resumed` defensiv lesen (`if (r && r.resumed)`) — ältere Clients
ohne das Feld bleiben kompatibel (additive API-Änderung).

## Runs

`runs/<projekt>/<id>.json` (maschinell: Status, Steps, Doku) +
`runs/<projekt>/<id>.log` (menschenlesbar, Live). Step-Output wird auf
200 KB gekappt. Status: `running`, `pass`, `fail`, `needs_approval`,
`rejected`. Das rohe `.log` wird per `GET /api/run/<id>/log` ausgeliefert
und im Run-Panel per **📜 log**-Button auf Abruf angezeigt (Toggle zwischen
formatierter Record-Ansicht und rohem Log).

**Single-Node:** `fabrik serve` setzt beim Start jeden `running`-Record auf `error`
(Restart-Recovery, `needs_approval` bleibt). Zwei Server-Prozesse mit gemeinsamem
`runs/`-Verzeichnis werden daher nicht unterstützt.

Agent-Steps schreiben ihre **Konversation** ins Log: den Prompt (Input,
gekappt), die Assistant-Texte und Tool-Calls (live, je abgeschlossener
Message) sowie den Final-Text (Output). So ist nach einem Run nachvollziehbar,
was der Agent bekommen hat, was er getan und was er geantwortet hat.

### Fehlerklassen (ADR 0035)

Gescheiterte Runs (`fail`/`error`) und Steps tragen `failure_kind`; die UI zeigt
sie als Badge (Loop-Liste, Historie, Run-Modal), die Historie filtert danach.

| Klasse | Bedeutung | Beispiele |
|---|---|---|
| `interrupted` | von außen abgebrochen | Server-Neustart (Reaper), Dead-Man's-Switch |
| `transient` | externe Abhängigkeit kurz weg | Exit 75 (`EX_TEMPFAIL`), Netzfehler, HTTP 429/5xx (außer 501/505), `git fetch` ohne Netz, Agent-Backend nicht erreichbar, hängende Session |
| `environment` | Umgebung/Konfiguration kaputt — braucht einen Menschen | Exit 126/127/78 (`EX_CONFIG`), fehlende Datei, ungültige Step-Konfiguration, Projekt/Loop unbekannt, HTTP 401/403, Token fehlt |
| `outcome` | echtes negatives Ergebnis | Tests rot (`failure_kind: outcome` am Step), Review `changes_requested`, Merge abgelehnt |
| `unknown` | nicht klassifiziert (Default, auch Altbestand) | alles andere |

Klassifiziert wird an der Quelle (`src/fabrik/failures.py`): Steps melden die
Klasse im Ergebnis oder werfen typisierte Fehler; Text-Muster gibt es nur als
Fallback für fremde CLI-Ausgaben (Agent-CLIs, `git`). Bei `transient` kann ein
Run `retry_after` (UTC) tragen, z. B. aus `Retry-After`. Die Klasse ändert das
Ablaufverhalten nicht; Auto-Retry baut darauf auf (ADR 0036).

### Verdikt (fachliches Ergebnis)

Der Run-Status sagt, ob die Maschinerie lief; ein Step kann trotzdem ein negatives *Ergebnis*
melden — z. B. `relay-audit`: `audit-publish` läuft grün und veröffentlicht `fail`. Ein
shell-Step meldet das mit einer eigenen Ausgabezeile `::fabrik-verdict::<wort>` (a–z, `_`, `-`,
max. 20 Zeichen). Die Zeile verschwindet aus Output und Summary und landet als `verdict` im
Step-Record; das Verdikt des Runs ist das des letzten Steps, der eins gemeldet hat. Die UI zeigt es
als Badge neben dem Status (Liste, Historie, Run-Detail, Step): `pass`/`done` grün, `fail`/`blocked`
rot, sonst neutral. Ablauf, Retry und Alerts bleiben unberührt. Relay meldet `pass`/`fail` (Audit)
bzw. den Handoff-Status (`active`/`blocked`/`done`, Session).

### Run-Retry (ADR 0036)

Gescheiterte Runs werden optional automatisch wiederholt — pro
`(Projekt, Loop, Trigger)` eine **Retry-Kette** mit Backoff. Standardmäßig aus;
aktiviert wird er per `retry:`-Block auf Projekt- oder Loop-Ebene (Loop ersetzt
den Projekt-Block vollständig, `max: 0` schaltet ab):

```yaml
retry:
  max: 3                      # zusätzliche Versuche nach dem ersten Run
  backoff: [5m, 30m, 2h]      # Wartezeit vor Versuch 1..n (letzter gilt weiter)
  on: [interrupted, transient]  # nur diese Klassen werden wiederholt
  max_wait: 24h               # Obergrenze für ein gemeldetes `retry_after`
```

Nur Klassen aus `on` (immer eine Teilmenge von `interrupted`/`transient`)
lösen einen Retry aus; `unknown`/`outcome`/`environment` enden die Kette.
Jeder Versuch durchläuft alle Gates neu (Approval, Blocker) und holt frische
Trigger-Variablen von GitHub. Erschöpft die Kette das Budget, wird der Auftrag
in der UI unter **📦 Abgelegt** gelistet (Klasse, letzter Fehler, Versuchszahl)
mit den Aktionen **↻ jetzt erneut** (Run mit vollem Budget; ohne freien Slot
wartet er mit Vorrang wie andere manuelle Runs) und
**⊘ verwerfen**. Ein laufender Cron-Tick wird übersprungen, solange die Kette
`pending` ist; `reconcile` zählt jeden Run weiterhin als erledigt.
Ein geschlossenes Issue bzw. ein gemergter/geschlossener PR beendet die Kette
(`dismissed`) — außer der `on_pr`-Trigger feuert selbst auf geschlossene PRs
(`states: [.., closed]`); ein Restart während eines Versuchs verbraucht ihn (`interrupted`).
Solange die Kette `pending` ist, gibt es kein `run_failed`; der endgültige
Fehlschlag kommt als `run_exhausted` — auch bei `events: [run_failed]`.

**Einmaliger Bestandsabgleich** (`fabrik retry-backfill`, idempotent): legt für
Trigger, deren *komplette* Runs gescheitert sind (Bestand vor Einführung von
`retry`), eine Kette an — liegt die Klasse des letzten Runs in `on` der Policy
des Loops → `pending` (startet beim nächsten Scheduler-Tick nach dem Backoff;
ein `retry_after` des letzten Runs zählt, auf `max_wait` gekappt), jede andere
Klasse → `exhausted` (landet in der Liste „Abgelegt“ zur Durchsicht). Die
Altruns zählen nicht gegen das Budget — der erste Versuch ist Versuch 1 mit
vollem Budget. Ob der Trigger noch existiert (Issue zu, PR gemergt), prüft der
Abgleich nicht: der Versuch fragt GitHub beim ersten Tick ab und legt die
Kette dann als `dismissed` ab. Vorhandene Ketten und Gruppen mit
nicht-fehlgeschlagenen Runs bleiben unberührt.

### Run-Store (ADR 0034)

Runs werden nur über den Run-Store gelesen und geschrieben (`fabrik.runstore.STORE`);
das Backend wählt `FABRIK_RUN_STORE`:

- `sql` (Default): relationales Schema (SQLAlchemy Core + Alembic) unter `FABRIK_DB_URL`,
  Default `sqlite:///$FABRIK_HOME/state/fabrik.db` (WAL, `busy_timeout`,
  Fremdschlüssel). Tabellen für Runs, Steps, Usage, Review-Ergebnisse, Verify-Checks,
  Approvals, Run-Variablen und das Session-Log (`run_events`, `run_artifacts`,
  `run_checkpoints`); Status, Pflichtfelder und Fremdschlüssel prüft die DB. Das
  Schema migriert beim ersten Zugriff automatisch auf `head` (`src/fabrik/migrations`).
  Die Run-Suche ist ein indiziertes `SELECT`. Run-Logs (`runs/<projekt>/<id>.log`)
  bleiben Dateien.
- `json`: die frühere Ablage `runs/<projekt>/<id>.json` (Rückfall, Tests).

Bestehende Run-JSON und `sessionlog.db` übernimmt `fabrik migrate-runs` (erst
`--dry-run`: schreibt nichts und listet jeden nicht abbildbaren oder abweichenden
Record mit Run-Id und Grund; idempotent). Nach einem echten Import werden die
übernommenen Run-JSON nach `state/runs-archive/` verschoben (nie gelöscht; Layout
wie `runs/`) — defekte oder abweichende Records bleiben in `runs/` liegen. Sessions
ohne Run-Record (z. B. Test-Altlasten) werden gezählt, nicht übernommen.

Im SQL-Store sind Runs nicht mehr per `cat`/`jq` lesbar: `fabrik runs show <lauf_id>`
zeigt einen Einzelrun (Felder, Steps, Session-Log-Zählungen), `fabrik runs export
<lauf_id>` gibt Record + Session-Log als JSON aus.

### Session-Log (SQLite, ADR 0002)

Im SQL-Store (Default) liegt das Session-Log im Run-Store (siehe oben); das
Folgende beschreibt den JSON-Betrieb.

Neben den Run-JSONs liegt ein append-only Event-Store:
`$FABRIK_HOME/sessionlog.db` (Override: `FABRIK_SESSIONLOG`), WAL-Mode.
Tabellen: `sessions` (eine Zeile pro Run), `events`
(`run_start`/`step_start`/`step_done`/`tool_call`/`message`/`cost`/
`approval`/`run_end`), `artifacts`, `checkpoints` (nur `pass`-Steps).

Die Run-JSON bleibt Single Source of Truth für die Engine — das Log ist
eine durable, querybare Projektion (Rohdaten: Tokens + Cost vom Agent,
keine Preistabelle). Agent-Steps landen zusätzlich Tool-Call-Events pro
neu gesehendem Tool. Bestehende P0-Runs importieren sich per
`fabrik import-runs` (idempotent, `source='import'` — native Sessions
gewinnen). Log-Fehler fallen keinen Run (best-effort).

## SonarQube-Scan

`scripts/sonar-scan.sh` scannt den Code (pysonar) und prüft das Quality Gate
(täglich auf main, Loop `sonar-scan`; PR-Analyse via Community-Branch-Plugin,
Loop `sonar-pr`). Der tägliche main-Scan räumt zusätzlich verwaiste
Legacy-PR-Projekte auf (`scripts/sonar-pr-cleanup.py`, ADR 0053): Vor der
nativen PR-Analyse (#524) wurde pro PR ein eigenes Projekt
`quark-playground:fabrik:pr-<n>` angelegt. Alle `…:pr-<n>`-Projekte werden
gelistet und gelöscht, deren PR auf GitHub nicht mehr offen ist. Fail-safe:
unbestimmbarer PR-Status → Projekt bleibt; ein Fehlschlag des Aufräumens ist
nur eine Warnung und ändert den Exit-Code des Scans nicht.
`scripts/sonar-pr-cleanup.py --dry-run` zeigt, was gelöscht würde.

## Deployment-Profil (ADR 0056)

`--profile homelab|cloud` (global, vor dem Subcommand) oder `FABRIK_PROFILE`; Default `homelab`,
CLI gewinnt vor Env. Das Profil wählt beim Start pro Port den Adapter (`fabrik.profiles`):
`homelab` → lokaler Store, `cloud` → Platzhalter (`NotImplementedError`, bis die Folge-Issues
landen). Neue Ports/Adapter werden in `profiles._build_default_registry` registriert; die
Business-Logik kennt den Profilnamen nicht (Guard-Test in `tests/test_profiles.py`).

## Sicherheit

- Server bindet **standardmäßig nur 127.0.0.1**; andere Adressen nur mit `FABRIK_AUTH=local` oder `oidc`
- API gegen Cross-Origin gehärtet: `POST`-Requests werden nur aus dem eigenen
  Origin akzeptiert (kein `Access-Control-Allow-Origin: *`), damit eine
  beliebige Webseite im Browser des Operators keine Shell-Steps feuern kann
  (Drive-by-RCE). Optional: `FABRIK_TOKEN` erzwingt ein Bearer-Token für alle
  `POST`-Endpunkte.
- Push auf `main`/`master` (oder `protected:`-Liste) wird hart verweigert
- Substituierte Branch-/Head-Namen (`git`-/`pr`-Step) werden vor Git-/API-Aufrufen
  validiert (`validate_branch_name`: Allowlist `[A-Za-z0-9._/-]`, kein `..`, kein
  führendes `-`, Max-Länge)
- Kein Step kann aus `workdir`/`WEB_DIR` herauslesen (Path-Traversal-Check);
  `report.write`-Pfade müssen in der Projekt-Root bleiben
- LLM-Steps injizieren nur Schema-Keys in den ctx (keine Überschreibung
  vertrauenswürdiger Variablen — Untrusted-Input bleibt Daten)
- Secrets werden aus Logs **und** Run-Records gemaskt (env + `fabrik.env` +
  Spec-Env-Werte); Git-Fehlermeldungen sind redacted
- SonarQube: nur den gecachten Token aus `state/sonar-token.txt` verwenden
  (`state/` und `fabrik.env` sind gitignored); `SONAR_ADMIN_PASSWORD` dient
  ausschließlich zum Minten eines frischen Tokens — niemals in Repo, Issues,
  Commits oder Logs schreiben
- `send`-Loops (irreversibel, z. B. Signal) sind `manual`-only
- Spec-Änderungen aus dem LLM-Chat werden nur nach `apply` geschrieben
- Agent-Containment: File-Tools Workdir-gefangen; Shell-Zugriff erfordert
  non-root/Container-Deployment für echte Isolation

### Label-Migration (`ready to develop` → `ready for development`)

Bestehende Issues einmalig umlabeln (benötigt `gh`, Label vorher anlegen):

```bash
gh label create "ready for development" --repo OWNER/REPO || true
for n in $(gh issue list --repo OWNER/REPO --label "ready to develop" --state all --limit 500 --json number -q '.[].number'); do
  gh issue edit "$n" --repo OWNER/REPO --add-label "ready for development" --remove-label "ready to develop"
done
gh label delete "ready to develop" --repo OWNER/REPO --yes   # optional, danach
```

## Verwendete Projekte

- `kyudo-neandertal` (privat, `FABRIK_DATA`) — Drupal 7→11 Migration (lint=CI-Checks, migrate=Issue→PR
  mit Approval-Gate, `github_issue`-Alerts bei Fehlern)
- `echochamber` — Meeting-Pipeline (lint/smoke, Worktree auf M3-Branch)
- `gods-last-garden` (privat, `FABRIK_DATA`) — Godot-FPS (lint=Import, test=Headless-Batterie)

## Roadmap

- [x] `agent`-Steps mit opencode-Backend (P1; SPIKE: `docs/spike-opencode.md`)
- [x] SQLite Session-Log (End-Schema, ADR 0002; `fabrik import-runs`)
- [x] `review`-Step: Cross-Model Cold Critic (ADR 0003)
- [ ] Weitere Agent-Backends (claude/codex) über dasselbe Interface
- [ ] Loop-Dependencies (Loop A muss vor Loop B grün sein)
- [x] Bounded Worker-Pool für Trigger-Runs (`FABRIK_WORKERS`; lokal = 1)
- [ ] Spec-Editor in der UI (direkt, ohne Chat)
