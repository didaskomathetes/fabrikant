#!/usr/bin/env bash
# SonarQube-Scan (Python via pysonar) — Default: das fabrik-Projekt; andere Projekte über SONAR_PROJECT_DIR & Co.
#
# Ablauf:
#   1. SonarQube (compose.yaml) starten, falls nicht erreichbar
#   2. Scanner-Token laden/erzeugen (cachen; Admin-Creds nur für den Erstlauf)
#   3. Auf Readiness warten (api/system/health → GREEN, Auth via Token)
#   3b. Bei PR-Runs (SONAR_PR_HEAD_REF) den PR-Head im Worktree auschecken (detached)
#   4. Tests + Coverage-Report (SONAR_TEST_CMD → Cobertura-XML)
#   4b. Analyzer-Reports (SONAR_LINT_CMD: ruff, bandit, pyright → externe Issues in Sonar)
#   5. Scanner ausführen (pysonar liest [tool.sonar] aus pyproject.toml, SONAR_SCANNER_ARGS ergänzen;
#      Coverage via sonar.python.coverage.reportPaths)
#   6. Quality Gate prüfen (warte auf Analyse-Completion, Status muss OK sein)
#   7. (nur main-Scan, SONAR_PR leer) Verwaiste Legacy-PR-Projekte aufräumen (ADR 0053, advisory):
#      `scripts/sonar-pr-cleanup.py` löscht `<PROJECT_KEY>:pr-<n>`-Projekte aus der Zeit vor
#      der nativen PR-Analyse, deren PR gemergt/geschlossen ist. Fehlschlag = nur Warnung —
#      der Exit-Code des Scans (Quality Gate) bleibt unberührt.
#
# Env-Overrides:
#   SONAR_HOST_URL        (Default: http://127.0.0.1:9000)
#   SONAR_ADMIN_USER      (Default: admin)
#   SONAR_ADMIN_PASSWORD  (Default: admin — nur relevant, wenn kein Token gecacht ist)
#   SONAR_COMPOSE_FILE    (Default: <fabrik>/compose.yaml)
#   SONAR_PROJECT_KEY     (Default: fabrik)
#   SONAR_PROJECT_DIR     zu scannendes Repo/Verzeichnis (Default: <fabrik>; relativ = zum Aufruf-cwd)
#   SONAR_TEST_CMD        Tests + Coverage, `bash -c` im Projekt (Default: fabrik-pytest → coverage.xml);
#                         Exit 1/2 = Tests rot
#   SONAR_COVERAGE_PATHS  Cobertura-Reports für Sonar, Komma/Wildcards (Default: coverage.xml);
#                         leer gesetzt = nicht übergeben (sonar-project.properties des Projekts gilt)
#   SONAR_LINT_CMD        schreibt Analyzer-Reports nach $SONAR_LINT_DIR, `bash -c` im Projekt (Default für fabrik:
#                         scripts/lint.sh --reports; für fremde Projekte (SONAR_PROJECT_DIR) leer = kein Import).
#                         Erkannte Dateien → Scanner-Parameter: ruff.json → sonar.python.ruff.reportPaths,
#                         bandit.json → sonar.python.bandit.reportPaths, pyright-sonar.json (Generic Issue Format)
#                         → sonar.externalIssuesReportPaths. Exit ≠ 0 = Setup-Fehler (Funde sind keiner).
#   SONAR_LINT_DIR        Report-Verzeichnis, relativ zum Projekt (Default: .fabrik/sonar/lint; wird vorab geleert)
#   SONAR_SCANNER         Scanner-Befehl (Default: `uv run pysonar` im Projekt; fremde Projekte ohne
#                         pysonar-Dependency: <fabrik>/.venv/bin/pysonar; oder das eigene Scan-Skript des
#                         Projekts — es bekommt die -D-Parameter als Argumente und muss sie an den Scanner
#                         reichen). Exit ≠ 0 NACH dem Upload (CE-Task im Output, z. B. Tests rot): Gate und
#                         Report laufen trotzdem, Ergebnis Exit 1.
#   SONAR_SCANNER_ARGS    zusätzliche `-Dsonar.…=…`-Parameter, durch Leerzeichen getrennt (ohne
#                         [tool.sonar]: mind. -Dsonar.projectKey=… -Dsonar.sources=…)
#   SONAR_READY_TIMEOUT   (Default: 180 s)
#   SONAR_PR_HEAD_REF     (Branch des PR-Heads, vom on_pr-Trigger via `env:` gesetzt; leer oder
#                          ein unaufgelöster Platzhalter `{…}` (Cron-Run) = kein PR → Default-Checkout)
#   SONAR_BRANCH          Branch-Analyse (Community-Branch-Plugin, compose.yaml):
#                         Branch-Name oder `auto` (= aktueller Git-Branch). Default: leer → Main-Branch.
#   SONAR_PR              PR-Analyse: PR-Nummer oder `auto` (= PR des aktuellen Branches, via gh).
#                         Ist das Projekt an eine GitHub-App gebunden (scripts/sonar-github-setup.sh),
#                         dekoriert SonarQube den PR (Check Run + Summary-Kommentar; braucht Checks-Permission).
#                         Hat Vorrang vor SONAR_BRANCH. Der lokale HEAD sollte dem PR-Head entsprechen.
#                         Unaufgelöste Platzhalter `{…}` (Cron-Run) gelten bei beiden als nicht gesetzt.
#   SONAR_REPORT_FILE     Ergebnis (Gate, Kennzahlen, offene Issues) zusätzlich als JSON schreiben —
#                         auch bei rotem Gate.
#   SONAR_COMMENT_FILE    mit SONAR_REPORT_FILE: zusätzlich den PR-Kommentar als Markdown rendern
#   SONAR_LINK_BASE       Pflicht zu SONAR_COMMENT_FILE: https://github.com/<repo>/blob/<sha> (Issue-Links)
#
# Beispiele:
#   SONAR_BRANCH=auto bash scripts/sonar-scan.sh   # lokal vor dem Push
#   SONAR_PR=42 bash scripts/sonar-scan.sh         # Review von PR #42 (vorher `gh pr checkout 42`)
#
# PR-Modus (advisory, für extra_context in pr_review):
#   sonar-scan.sh --pr <n> [--post] [--base <branch>]
#   Scan der aktuellen Workdir unter eigenem Key <PROJECT_KEY>:pr-<n> (Community
#   Edition kann keine PR-Analyse; die main-Analyse bleibt unberührt). Ein
#   coverage.xml, das jünger als der HEAD-Commit ist, wird wiederverwendet. Statt
#   des Quality Gates (auf einem frischen Key bedeutungslos) schreibt
#   scripts/sonar-pr-report.py den Report nach stdout (Sonar-Issues, Lint via
#   ruff + mypy und Diff-Coverage — jeweils nur auf den geänderten Zeilen);
#   --post legt ihn zusätzlich als PR-Kommentar an bzw. aktualisiert ihn.
#
# Logs gehen nach stderr, stdout trägt nur den PR-Report.
#
# Exit-Codes (Fehlerklasse nach ADR 0035, sysexits.h):
#   0  Quality Gate OK (main) bzw. Report erzeugt (PR-Modus)
#   1  Quality Gate != OK oder Tests rot (Spec: failure_kind: outcome)
#   75 EX_TEMPFAIL — SonarQube nicht erreichbar/bereit, Analyse abgebrochen, PR-Head nicht abrufbar (→ transient)
#   78 EX_CONFIG   — Docker-Start, Admin-Creds, Scanner-Token oder Scanner-Output passen nicht, sonstiger
#                    unerwarteter Skriptfehler (→ environment)
#   126/127        — Tool nicht ausführbar/fehlt (z. B. pysonar) (→ environment)
#   >128           — pytest/pysonar durch Signal beendet (→ unknown)
set -euo pipefail

EX_GATE_RED=1
EX_TEMPFAIL=75
EX_CONFIG=78
EX_NOEXEC=126
EX_NOTFOUND=127
EX_SIGNAL_BASE=128

# Unerwartet gescheiterter Befehl (set -e): sonst Exit 1 = „Gate/Tests rot“ (outcome) für einen Setup-Fehler.
trap 'rc=$?; echo "[sonar-scan] FEHLER: unerwarteter Fehler (exit $rc, Zeile $LINENO)"; exit "$EX_CONFIG"' ERR

FABRIK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_URL="${SONAR_HOST_URL:-http://127.0.0.1:9000}"
ADMIN_USER="${SONAR_ADMIN_USER:-admin}"
ADMIN_PASS="${SONAR_ADMIN_PASSWORD:-admin}"
COMPOSE_FILE="${SONAR_COMPOSE_FILE:-$FABRIK_DIR/compose.yaml}"
PROJECT_KEY="${SONAR_PROJECT_KEY:-fabrik}"
PROJECT_DIR="$(cd "${SONAR_PROJECT_DIR:-$FABRIK_DIR}" && pwd)"
DEFAULT_TEST_CMD='uv run pytest -m "not browser and not perf" --cov=src --cov-report=xml:coverage.xml -q'
TEST_CMD="${SONAR_TEST_CMD:-$DEFAULT_TEST_CMD}"
COVERAGE_PATHS="${SONAR_COVERAGE_PATHS-coverage.xml}"  # leer gesetzt = Projekt-Config gilt
DEFAULT_LINT_CMD=""
[ "$PROJECT_DIR" = "$FABRIK_DIR" ] && DEFAULT_LINT_CMD='bash scripts/lint.sh --reports "$SONAR_LINT_DIR"'
LINT_CMD="${SONAR_LINT_CMD-$DEFAULT_LINT_CMD}"  # leer gesetzt = keine Analyzer-Reports
LINT_DIR="${SONAR_LINT_DIR:-.fabrik/sonar/lint}"
# Report-Datei (von SONAR_LINT_CMD) → Scanner-Parameter, der sie importiert.
LINT_REPORTS=(
  "ruff.json=sonar.python.ruff.reportPaths"
  "bandit.json=sonar.python.bandit.reportPaths"
  "pyright-sonar.json=sonar.externalIssuesReportPaths"
)
SCANNER="${SONAR_SCANNER:-}"
read -ra EXTRA_SCANNER_ARGS <<< "${SONAR_SCANNER_ARGS:-}"
# Token-File: über SONAR_TOKEN_FILE teilbar (z. B. Haupt-Repo statt Worktree,
# damit Worktree-Runs denselben Token wiederverwenden und keine Token-Flut erzeugen).
TOKEN_FILE="${SONAR_TOKEN_FILE:-$FABRIK_DIR/state/sonar-token.txt}"
READY_TIMEOUT="${SONAR_READY_TIMEOUT:-180}"
BRANCH="${SONAR_BRANCH:-}"
PR="${SONAR_PR:-}"
AUTO=auto
REPORT_FILE="${SONAR_REPORT_FILE:-}"
COMMENT_FILE="${SONAR_COMMENT_FILE:-}"
REPORT_METRICS="new_coverage,new_duplicated_lines_density,new_violations,new_security_hotspots,coverage"
REPORT_MAX_ISSUES=100
# Unaufgelöste Spec-Platzhalter (`{pr_number}` im Cron-Run) heißen „nicht gesetzt“.
case "$BRANCH" in "{"*) BRANCH="" ;; esac
case "$PR" in "{"*) PR="" ;; esac

# PR-Modus (advisory, CLI): --pr <n> [--post] [--base <branch>] — eigener Key, Report statt Gate.
# Unabhängig von SONAR_PR (native PR-Analyse): CLI-Modus scannt unter <PROJECT_KEY>:pr-<n>.
CLI_PR=""
POST=""
BASE="main"
while [ $# -gt 0 ]; do
  case "$1" in
    --pr)
      [ $# -ge 2 ] || { echo "[sonar-scan] --pr erwartet eine PR-Nummer" >&2; exit 2; }
      CLI_PR="$2"; shift 2 ;;
    --post) POST="--post"; shift ;;
    --base)
      [ $# -ge 2 ] || { echo "[sonar-scan] --base erwartet einen Branch-Namen" >&2; exit 2; }
      BASE="$2"; shift 2 ;;
    *) echo "[sonar-scan] unbekannte Option: $1" >&2; exit 2 ;;
  esac
done
if [ -n "$CLI_PR" ] && ! [[ "$CLI_PR" =~ ^[0-9]+$ ]]; then
  echo "[sonar-scan] --pr erwartet eine PR-Nummer (ist: $CLI_PR)" >&2
  exit 2
fi

if [ -n "$CLI_PR" ]; then
  PROJECT_KEY="$PROJECT_KEY:pr-$CLI_PR"
fi

# PR-Modus: Logs nach stderr (stdout trägt nur den PR-Report); Default: stdout (wie main).
log() {
  if [ -n "$CLI_PR" ]; then
    echo "[sonar-scan] $*" >&2
  else
    echo "[sonar-scan] $*"
  fi
}

# 1) SonarQube starten (falls nicht up)
# (api/system/status braucht keine Auth — api/system/health antwortet ohne Token immer 401.)
if ! curl -sf -o /dev/null --max-time 3 "$HOST_URL/api/system/status"; then
  log "SonarQube nicht erreichbar → docker compose up -d ($COMPOSE_FILE)"
  if ! docker compose -f "$COMPOSE_FILE" up -d; then
    log "FEHLER: docker compose up fehlgeschlagen"
    exit "$EX_CONFIG"
  fi
fi

# 2) Token (cachen, sonst neu erzeugen).
#    Vor der Readiness-Prüfung, damit der gecachte Token auch als Auth-Quelle
#    dient — das Admin-Passwort ändert sich nach dem First-Login und ist
#    danach (und z. B. in Worktrees) nicht mehr bekannt.
mkdir -p "$(dirname "$TOKEN_FILE")"
if [ -s "$TOKEN_FILE" ]; then
  TOKEN="$(cat "$TOKEN_FILE")"
  log "Nutze gecachten Scanner-Token"
else
  log "Erzeuge Scanner-Token (Admin-Creds: $ADMIN_USER) …"
  rc=0
  resp="$(curl -sf --max-time 10 -u "$ADMIN_USER:$ADMIN_PASS" \
    -X POST "$HOST_URL/api/user_tokens/generate?name=fabrik-scanner")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    # curl 22 = HTTP-Fehler (z. B. 401: Admin-Passwort geändert) → Mensch; sonst Netz/Timeout.
    log "FEHLER: Token-Erzeugung fehlgeschlagen (curl exit $rc)"
    if [ "$rc" -eq 22 ]; then exit "$EX_CONFIG"; fi
    exit "$EX_TEMPFAIL"
  fi
  if ! TOKEN="$(printf '%s' "$resp" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)"; then
    log "FEHLER: Antwort der Token-Erzeugung enthält keinen Token"
    exit "$EX_CONFIG"
  fi
  printf '%s' "$TOKEN" > "$TOKEN_FILE"
  log "Token erzeugt"
fi

# 3) Auf Readiness warten (health=GREEN, Basic-Auth mit Scanner-Token).
#    401/403: Token widerrufen bzw. SonarQube zurückgesetzt — Warten heilt das nicht (→ environment).
log "Warte auf SonarQube-Readiness (max. ${READY_TIMEOUT}s) …"
status=""
for i in $(seq 1 "$READY_TIMEOUT"); do
  resp="$(curl -s --max-time 3 -u "$TOKEN:" -w '\n%{http_code}' "$HOST_URL/api/system/health" 2>/dev/null || true)"
  http_code="${resp##*$'\n'}"
  if [ "$http_code" = 401 ] || [ "$http_code" = 403 ]; then
    log "FEHLER: Scanner-Token abgelehnt (HTTP $http_code) — $TOKEN_FILE löschen, dann neu erzeugen lassen"
    exit "$EX_CONFIG"
  fi
  status="$(printf '%s' "${resp%$'\n'*}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("health",""))' 2>/dev/null || true)"
  if [ "$status" = "GREEN" ]; then
    log "SonarQube ist bereit (GREEN)"
    break
  fi
  if [ "$i" = "$READY_TIMEOUT" ]; then
    log "FEHLER: SonarQube nach ${READY_TIMEOUT}s nicht GREEN (letzter Status: ${status:-?})"
    exit "$EX_TEMPFAIL"
  fi
  sleep 1
done

# 3b) PR-Run: Run-Worktrees stehen detached auf origin/<repo.branch> — ohne Checkout würde der PR-Trigger
#     main scannen. Leer oder mit `{` beginnend (unaufgelöster Platzhalter, Cron) = kein PR.
PR_HEAD_REF="${SONAR_PR_HEAD_REF:-}"
case "$PR_HEAD_REF" in "{"*) PR_HEAD_REF="" ;; esac
if [ -n "$PR_HEAD_REF" ]; then
  log "PR-Run: checke PR-Head '$PR_HEAD_REF' aus (detached)"
  if ! git -C "$PROJECT_DIR" fetch --quiet origin "refs/heads/$PR_HEAD_REF" \
    || ! git -C "$PROJECT_DIR" checkout --quiet --detach FETCH_HEAD; then
    log "FEHLER: PR-Head '$PR_HEAD_REF' nicht abrufbar/auscheckbar (Branch gelöscht? Netzwerk?)"
    exit "$EX_TEMPFAIL"
  fi
fi

# Analyse-Ziel auflösen → SCAN_ARGS (pysonar-Parameter) + GATE_QUERY (Filter für project_status).
# Nach dem PR-Head-Checkout (3b, HEAD-Vergleich) und vor dem teuren Testlauf (Aufruffehler früh).
SCAN_ARGS=()
GATE_QUERY=""
if [ "$BRANCH" = "$AUTO" ] || [ "$PR" = "$AUTO" ]; then
  BRANCH_AUTO="$(git -C "$PROJECT_DIR" symbolic-ref --short -q HEAD || true)"
  if [ -z "$BRANCH_AUTO" ]; then
    log "FEHLER: SONAR_BRANCH/SONAR_PR=auto, aber HEAD ist detached"
    exit "$EX_CONFIG"
  fi
  [ "$BRANCH" = "$AUTO" ] && BRANCH="$BRANCH_AUTO"
fi
if [ -n "$PR" ]; then
  pr_ref="$PR"; [ "$PR" = "$AUTO" ] && pr_ref="$BRANCH_AUTO"
  if ! pr_info="$(cd "$PROJECT_DIR" && gh pr view "$pr_ref" --json number,headRefName,baseRefName,headRefOid \
      -q '[.number, .headRefName, .baseRefName, .headRefOid] | @tsv' 2>/dev/null)"; then
    log "FEHLER: PR '$pr_ref' nicht gefunden (gh installiert/eingeloggt?)"
    exit "$EX_CONFIG"
  fi
  IFS=$'\t' read -r PR PR_HEAD PR_BASE PR_SHA <<< "$pr_info"
  if [ "$(git -C "$PROJECT_DIR" rev-parse HEAD)" != "$PR_SHA" ]; then
    log "WARNUNG: lokaler HEAD ≠ PR-Head ($PR_SHA) — Kommentare können auf falschen Zeilen landen (gh pr checkout $PR)"
  fi
  SCAN_ARGS=("-Dsonar.pullrequest.key=$PR" "-Dsonar.pullrequest.branch=$PR_HEAD" "-Dsonar.pullrequest.base=$PR_BASE")
  GATE_QUERY="&pullRequest=$PR"
  log "Ziel: PR #$PR ($PR_HEAD → $PR_BASE)"
elif [ -n "$BRANCH" ]; then
  SCAN_ARGS=("-Dsonar.branch.name=$BRANCH")
  GATE_QUERY="&branch=$BRANCH"
  log "Ziel: Branch $BRANCH"
fi

# 4) Tests + Coverage-Report (Cobertura-XML) — ohne Coverage-Daten scheitert der Quality Gate an
#    new_coverage (Default: >= 80 %). fabrik-Default: perf-Tests ausgenommen (wie CI), ihr
#    Wall-Clock-Budget bricht unter Coverage-Instrumentierung.
cd "$PROJECT_DIR"
# PR-Modus (advisory): frischer Coverage-Report (jünger als HEAD) wird wiederverwendet —
# der teure Testlauf entfällt, der Scan läuft nur für den Report. `git log` nur im
# PR-Modus (Bedingung): in fremden Projekt-Dirs ohne Git-Repo würde es sonst fehlschlagen.
if [ -n "$CLI_PR" ] && [ -f coverage.xml ] && [ "$(stat -c %Y coverage.xml)" -ge "$(git log -1 --format=%ct)" ]; then
  log "Nutze vorhandenen Coverage-Report (jünger als HEAD) — Tests übersprungen"
else
  log "Tests + Coverage-Report ($TEST_CMD) …"
  rc=0
  bash -c "$TEST_CMD" || rc=$?
  # pytest: 1 = Tests rot, 2 = Collection-Fehler/abgebrochen (Code-Ergebnis); 3–5 = interner/Aufruf-Fehler,
  # keine Tests (Setup); Signal (>128) unverändert weiterreichen.
  case "$rc" in
    0) ;;
    1|2) log "FEHLER: Tests rot (pytest exit $rc)"; exit "$EX_GATE_RED" ;;
    *)
      log "FEHLER: pytest fehlgeschlagen (exit $rc)"
      if [ "$rc" -gt "$EX_SIGNAL_BASE" ]; then exit "$rc"; fi
      exit "$EX_CONFIG"
      ;;
  esac
fi

# Keine Coverage-Datei → Sonar meldet 0 % nur als Analyse-Warnung; hier sichtbar machen. Kein Fehler:
# ein PR ohne betroffenes Testpaket erzeugt legitim keine.
COVERAGE_ARGS=()
if [ -n "$COVERAGE_PATHS" ]; then
  COVERAGE_ARGS=("-Dsonar.python.coverage.reportPaths=$COVERAGE_PATHS")
  IFS=',' read -ra cov_patterns <<< "$COVERAGE_PATHS"
  cov_found=""
  for pattern in "${cov_patterns[@]}"; do
    if compgen -G "$pattern" >/dev/null; then cov_found=1; fi
  done
  [ -n "$cov_found" ] || log "WARNUNG: kein Coverage-Report für '$COVERAGE_PATHS' in $PROJECT_DIR"
fi

# 4b) Analyzer-Reports (ruff/bandit/pyright) → externe Issues. Vorab leeren: kein Upload veralteter Reports.
LINT_ARGS=()
if [ -n "$LINT_CMD" ]; then
  rm -rf "$LINT_DIR"
  mkdir -p "$LINT_DIR"
  log "Analyzer-Reports ($LINT_CMD) …"
  rc=0
  SONAR_LINT_DIR="$LINT_DIR" bash -c "$LINT_CMD" || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "FEHLER: Analyzer-Reports fehlgeschlagen (exit $rc)"
    # 126/127 (Tool fehlt) und Signale unverändert; sonst Setup-Fehler — Funde selbst sind kein Exit ≠ 0.
    if [ "$rc" -eq "$EX_NOEXEC" ] || [ "$rc" -eq "$EX_NOTFOUND" ] || [ "$rc" -gt "$EX_SIGNAL_BASE" ]; then exit "$rc"; fi
    exit "$EX_CONFIG"
  fi
  for entry in "${LINT_REPORTS[@]}"; do
    file="$LINT_DIR/${entry%%=*}"
    if [ -s "$file" ]; then LINT_ARGS+=("-D${entry#*=}=$file"); fi
  done
  log "Analyzer-Reports: ${#LINT_ARGS[@]} von ${#LINT_REPORTS[@]} gefunden"
fi

# 5) Scan (pysonar liest [tool.sonar] aus pyproject.toml im PROJECT_DIR, falls vorhanden).
#    Output wird mitgeteet, damit wir die CE-Task-ID für Step 6 extrahieren
#    können (pysonar druckt: …/api/ce/task?id=<uuid>).
export SONAR_HOST_URL="$HOST_URL"
export SONAR_TOKEN="$TOKEN"
log "Führe Scanner aus (projectKey=$PROJECT_KEY) …"
# Scanner vorab prüfen: `uv run` meldet ein fehlendes Kommando mit Exit 2 — unten sähe das wie ein
# gescheiterter Upload (75, transient) aus statt wie ein Umgebungsfehler (127, ADR 0035).
if [ -n "$SCANNER" ]; then
  read -ra SCANNER_CMD <<< "$SCANNER"
  if ! command -v "${SCANNER_CMD[0]}" >/dev/null 2>&1; then
    log "FEHLER: Scanner nicht gefunden: ${SCANNER_CMD[0]} (SONAR_SCANNER)"
    exit "$EX_NOTFOUND"
  fi
elif uv run --no-sync sh -c 'command -v pysonar' >/dev/null 2>&1; then
  SCANNER_CMD=(uv run pysonar)
else
  log "FEHLER: pysonar nicht gefunden (uv sync / pyproject.toml prüfen)"
  exit "$EX_NOTFOUND"
fi
SCAN_LOG="$(mktemp)"
trap 'rm -f "$SCAN_LOG"' EXIT
rc=0
if [ -n "$CLI_PR" ]; then
  # PR-Modus: Logs nach stderr, stdout trägt nur den PR-Report (sonar-pr-report.py).
  "${SCANNER_CMD[@]}" "${COVERAGE_ARGS[@]}" "${LINT_ARGS[@]}" "${EXTRA_SCANNER_ARGS[@]}" "${SCAN_ARGS[@]}" 2>&1 \
    | tee "$SCAN_LOG" >&2 || rc=$?
else
  "${SCANNER_CMD[@]}" "${COVERAGE_ARGS[@]}" "${LINT_ARGS[@]}" "${EXTRA_SCANNER_ARGS[@]}" "${SCAN_ARGS[@]}" 2>&1 \
    | tee "$SCAN_LOG" || rc=$?
fi
TASK_ID="$(grep -oE 'api/ce/task\?id=[a-f0-9-]+' "$SCAN_LOG" | tail -1 | cut -d= -f2 || true)"
SCANNER_RC=0
if [ "$rc" -ne 0 ] && [ -n "$TASK_ID" ]; then
  # Upload gelungen, das Scan-Skript des Projekts meldet danach einen Fehler (z. B. Tests rot):
  # Gate + Report trotzdem (PR-Kommentar), am Ende Exit 1 statt „transient“.
  SCANNER_RC="$rc"
  log "WARNUNG: Scanner meldet exit $rc nach dem Upload — Analyse wird ausgewertet, Ergebnis rot"
elif [ "$rc" -ne 0 ]; then
  log "FEHLER: Scanner fehlgeschlagen (exit $rc)"
  # 126/127: Scanner nicht ausführbar (environment); Signal: unknown; sonst scheitert der Upload an SonarQube.
  if [ "$rc" -eq "$EX_NOEXEC" ] || [ "$rc" -eq "$EX_NOTFOUND" ] || [ "$rc" -gt "$EX_SIGNAL_BASE" ]; then exit "$rc"; fi
  exit "$EX_TEMPFAIL"
fi

# 6) Quality Gate prüfen.
#    WICHTIG: Zuerst auf CE-Task-Completion warten. Sonst liest die erste
#    Gate-Prüfung den STALEN Status der VORHERIGEN Analyse (Race: Upload ist
#    synchron, Processing asynchron) → falsches 'ERROR' trotz grünem Ergebnis.
#    Nach CE-SUCCESS ist der Gate-Status der aktuellen Analyse definitv.
if [ -z "$TASK_ID" ]; then
  log "FEHLER: keine CE-Task-ID im pysonar-Output gefunden"
  exit "$EX_CONFIG"  # Scanner-Output passt nicht zum Skript
fi
log "Warte auf Analyse-Completion (CE-Task $TASK_ID) …"
task_status=""
for i in $(seq 1 300); do
  task_status="$(curl -sf --max-time 10 -u "$TOKEN:" \
    "$HOST_URL/api/ce/task?id=$TASK_ID" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["task"]["status"])' 2>/dev/null || echo UNKNOWN)"
  case "$task_status" in SUCCESS|FAILED|CANCELED) break ;; esac
  sleep 1
done
log "CE-Task: $task_status"
if [ "$task_status" != "SUCCESS" ]; then
  log "FEHLER: Analyse-Task nicht SUCCESS ($task_status)"
  exit "$EX_TEMPFAIL"  # FAILED/CANCELED/nicht abrufbar: Server-Seite, kein Code-Ergebnis
fi

# PR-Modus (advisory, CLI): Report statt Gate.
if [ -n "$CLI_PR" ]; then
  git fetch -q origin "$BASE" >&2 || log "WARN: git fetch origin $BASE fehlgeschlagen — nutze lokalen Stand"
  uv run python scripts/sonar-pr-report.py --pr "$CLI_PR" --project-key "$PROJECT_KEY" --base "$BASE" $POST
  exit 0
fi

# Gate-Status (nach CE-Completion definitv; transiente Werte: UNKNOWN/NONE).
log "Prüfe Quality Gate …"
gate="UNKNOWN"
for i in $(seq 1 60); do
  gate="$(curl -sf --max-time 10 -u "$TOKEN:" \
    "$HOST_URL/api/qualitygates/project_status?projectKey=$PROJECT_KEY$GATE_QUERY" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["projectStatus"]["status"])' 2>/dev/null || echo UNKNOWN)"
  case "$gate" in OK|ERROR|WARN) break ;; esac
  sleep 1
done
log "Quality Gate: $gate"
if [ "$gate" = "UNKNOWN" ]; then
  log "FEHLER: Quality-Gate-Status nicht abrufbar"
  exit "$EX_TEMPFAIL"
fi

# 6b) Ergebnis-Report (SONAR_REPORT_FILE, optional SONAR_COMMENT_FILE) — vor der Gate-Bewertung, damit
#     auch ein rotes Gate im PR landet (Loop `sonar-pr`). Rendering: scripts/sonar_report.py (kein LLM).
#     Folge-Steps brauchen so keinen Sonar-Token.
if [ -n "$REPORT_FILE" ]; then
  RAW_DIR="$(mktemp -d)"
  sonar_get() { curl -sf --max-time 10 -u "$TOKEN:" -o "$RAW_DIR/$1.json" "$HOST_URL/api/$2"; }
  if ! { sonar_get gate "qualitygates/project_status?projectKey=$PROJECT_KEY$GATE_QUERY" \
      && sonar_get measures "measures/component?component=$PROJECT_KEY$GATE_QUERY&metricKeys=$REPORT_METRICS" \
      && sonar_get issues "issues/search?components=$PROJECT_KEY$GATE_QUERY&resolved=false&ps=$REPORT_MAX_ISSUES"; }; then
    log "FEHLER: Report-Daten nicht abrufbar"
    exit "$EX_TEMPFAIL"
  fi
  report_args=(--gate "$RAW_DIR/gate.json" --measures "$RAW_DIR/measures.json" --issues "$RAW_DIR/issues.json"
    --project-key "$PROJECT_KEY" --repo-prefix "$(git -C "$PROJECT_DIR" rev-parse --show-prefix)"
    --dashboard "$HOST_URL/dashboard?id=$PROJECT_KEY$GATE_QUERY" --report "$REPORT_FILE")
  if [ -n "$COMMENT_FILE" ]; then
    report_args+=(--comment "$COMMENT_FILE" --link-base "${SONAR_LINK_BASE:?SONAR_LINK_BASE fehlt (für SONAR_COMMENT_FILE)}")
  fi
  python3 "$FABRIK_DIR/scripts/sonar_report.py" "${report_args[@]}"
  rm -rf "$RAW_DIR"
  log "Report geschrieben: $REPORT_FILE${COMMENT_FILE:+ + Kommentar $COMMENT_FILE}"
fi

if [ "$gate" = "NONE" ]; then
  log "FEHLER: kein Quality Gate für $PROJECT_KEY konfiguriert"
  exit "$EX_CONFIG"
fi
if [ "$gate" != "OK" ]; then
  log "FEHLER: Quality Gate ist '$gate' (erwartet: OK)"
  exit "$EX_GATE_RED"
fi
if [ "$SCANNER_RC" -ne 0 ]; then
  log "FEHLER: Quality Gate OK, aber der Scanner meldete exit $SCANNER_RC (z. B. Tests rot)"
  exit "$EX_GATE_RED"
fi
log "SonarQube-Scan erfolgreich (Quality Gate OK)"

# 7) Verwaiste Legacy-PR-Projekte aufräumen (ADR 0053, advisory) — nur im main-Scan
#    (SONAR_PR leer), nicht bei PR-Scans: `<PROJECT_KEY>:pr-<n>`-Projekte aus der Zeit vor
#    der nativen PR-Analyse löschen, deren PR gemergt/geschlossen ist. Das Script ist
#    fail-safe (unbestimmbarer PR-Status → kein Löschen) und endet bei Fehlern mit Exit 0;
#    die zusätzliche `if !`-Wächterung stellt sicher, dass kein Fehlschlag den erfolgreichen
#    Scan bricht.
if [ -z "$PR" ]; then
  log "Räume verwaiste Sonar-PR-Projekte auf …"
  if ! uv run python scripts/sonar-pr-cleanup.py; then
    log "WARNUNG: Aufräumen der Sonar-PR-Projekte fehlgeschlagen (ignoriert)"
  fi
fi
