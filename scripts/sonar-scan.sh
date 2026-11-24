#!/usr/bin/env bash
# SonarQube scan (Python via pysonar) — default: the fabrikant project; other projects via SONAR_PROJECT_DIR & co.
#
# Flow:
#   1. Start SonarQube (compose.yaml) if not reachable
#   2. Load/create the scanner token (cache it; admin creds only for the first run)
#   3. Wait for readiness (api/system/health → GREEN, auth via token)
#   3b. For PR runs (SONAR_PR_HEAD_REF) check out the PR head in the worktree (detached)
#   4. Tests + coverage report (SONAR_TEST_CMD → Cobertura XML)
#   4b. Analyzer reports (SONAR_LINT_CMD: ruff, bandit, pyright → external issues in Sonar)
#   5. Run the scanner (pysonar reads [tool.sonar] from pyproject.toml, SONAR_SCANNER_ARGS add to it;
#      coverage via sonar.python.coverage.reportPaths)
#   6. Check the quality gate (wait for analysis completion, status must be OK)
#   7. (main scan only, SONAR_PR empty) Clean up orphaned legacy PR projects (ADR 0053, advisory):
#      `scripts/sonar-pr-cleanup.py` deletes `<PROJECT_KEY>:pr-<n>` projects from the time before
#      native PR analysis whose PR is merged/closed. Failure = warning only —
#      the exit code of the scan (quality gate) is unaffected.
#
# Env overrides:
#   SONAR_HOST_URL        (default: http://127.0.0.1:9000)
#   SONAR_ADMIN_USER      (default: admin)
#   SONAR_ADMIN_PASSWORD  (default: admin — only relevant if no token is cached)
#   SONAR_COMPOSE_FILE    (default: <fabrikant>/compose.yaml)
#   SONAR_PROJECT_KEY     (default: fabrikant)
#   SONAR_PROJECT_DIR     repo/directory to scan (default: <fabrikant>; relative = to the calling cwd)
#   SONAR_TEST_CMD        tests + coverage, `bash -c` in the project (default: fabrikant-pytest → coverage.xml);
#                         exit 1/2 = tests red
#   SONAR_COVERAGE_PATHS  Cobertura reports for Sonar, comma/wildcards (default: coverage.xml);
#                         set empty = not passed (the project's sonar-project.properties applies)
#   SONAR_LINT_CMD        writes analyzer reports to $SONAR_LINT_DIR, `bash -c` in the project (default for fabrikant:
#                         scripts/lint.sh --reports; for foreign projects (SONAR_PROJECT_DIR) empty = no import).
#                         Recognized files → scanner parameters: ruff.json → sonar.python.ruff.reportPaths,
#                         bandit.json → sonar.python.bandit.reportPaths, pyright-sonar.json (generic issue format)
#                         → sonar.externalIssuesReportPaths. Exit ≠ 0 = setup error (findings are not one).
#   SONAR_LINT_DIR        report directory, relative to the project (default: .fabrikant/sonar/lint; emptied beforehand)
#   SONAR_SCANNER         scanner command (default: `uv run pysonar` in the project; foreign projects without a
#                         pysonar dependency: <fabrikant>/.venv/bin/pysonar; or the project's own scan script —
#                         it receives the -D parameters as arguments and must pass them on to the
#                         scanner). Exit ≠ 0 AFTER the upload (CE task in the output, e.g. tests red): gate and
#                         report still run, result exit 1.
#   SONAR_SCANNER_ARGS    additional `-Dsonar.…=…` parameters, separated by spaces (without
#                         [tool.sonar]: at least -Dsonar.projectKey=… -Dsonar.sources=…)
#   SONAR_READY_TIMEOUT   (default: 180 s)
#   SONAR_PR_HEAD_REF     (branch of the PR head, set by the on_pr trigger via `env:`; empty or
#                          an unresolved placeholder `{…}` (cron run) = no PR → default checkout)
#   SONAR_BRANCH          branch analysis (community branch plugin, compose.yaml):
#                         branch name or `auto` (= current git branch). Default: empty → main branch.
#   SONAR_PR              PR analysis: PR number or `auto` (= PR of the current branch, via gh).
#                         If the project is bound to a GitHub App (scripts/sonar-github-setup.sh),
#                         SonarQube decorates the PR (check run + summary comment; needs checks permission).
#                         Takes precedence over SONAR_BRANCH. The local HEAD should match the PR head.
#                         Unresolved placeholders `{…}` (cron run) count as unset for both.
#   SONAR_REPORT_FILE     additionally write the result (gate, metrics, open issues) as JSON —
#                         even with a red gate.
#   SONAR_COMMENT_FILE    with SONAR_REPORT_FILE: additionally render the PR comment as Markdown
#   SONAR_LINK_BASE       required with SONAR_COMMENT_FILE: https://github.com/<repo>/blob/<sha> (issue links)
#
# Examples:
#   SONAR_BRANCH=auto bash scripts/sonar-scan.sh   # locally before pushing
#   SONAR_PR=42 bash scripts/sonar-scan.sh         # review of PR #42 (first `gh pr checkout 42`)
#
# PR mode (advisory, for extra_context in pr_review):
#   sonar-scan.sh --pr <n> [--post] [--base <branch>]
#   Scan of the current workdir under its own key <PROJECT_KEY>:pr-<n> (Community
#   Edition cannot do PR analysis; the main analysis stays untouched). A
#   coverage.xml younger than the HEAD commit is reused. Instead of
#   the quality gate (meaningless on a fresh key),
#   scripts/sonar-pr-report.py writes the report to stdout (Sonar issues, lint via
#   ruff + mypy and diff coverage — each only on the changed lines);
#   --post additionally creates it as a PR comment or updates it.
#
# Logs go to stderr, stdout carries only the PR report.
#
# Exit codes (error class per ADR 0035, sysexits.h):
#   0  quality gate OK (main) or report generated (PR mode)
#   1  quality gate != OK or tests red (spec: failure_kind: outcome)
#   75 EX_TEMPFAIL — SonarQube not reachable/ready, analysis aborted, PR head not fetchable (→ transient)
#   78 EX_CONFIG   — docker start, admin creds, scanner token or scanner output do not fit, other
#                    unexpected script error (→ environment)
#   126/127        — tool not executable/missing (e.g. pysonar) (→ environment)
#   >128           — pytest/pysonar terminated by a signal (→ unknown)
set -euo pipefail

EX_GATE_RED=1
EX_TEMPFAIL=75
EX_CONFIG=78
EX_NOEXEC=126
EX_NOTFOUND=127
EX_SIGNAL_BASE=128

# Unexpectedly failed command (set -e): otherwise exit 1 = "gate/tests red" (outcome) for a setup error.
trap 'rc=$?; echo "[sonar-scan] FEHLER: unerwarteter Fehler (exit $rc, Zeile $LINENO)"; exit "$EX_CONFIG"' ERR

FABRIKANT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_URL="${SONAR_HOST_URL:-http://127.0.0.1:9000}"
ADMIN_USER="${SONAR_ADMIN_USER:-admin}"
ADMIN_PASS="${SONAR_ADMIN_PASSWORD:-admin}"
COMPOSE_FILE="${SONAR_COMPOSE_FILE:-$FABRIKANT_DIR/compose.yaml}"
PROJECT_KEY="${SONAR_PROJECT_KEY:-fabrikant}"
PROJECT_DIR="$(cd "${SONAR_PROJECT_DIR:-$FABRIKANT_DIR}" && pwd)"
DEFAULT_TEST_CMD='uv run pytest -m "not browser and not perf" --cov=src --cov-report=xml:coverage.xml -q'
TEST_CMD="${SONAR_TEST_CMD:-$DEFAULT_TEST_CMD}"
COVERAGE_PATHS="${SONAR_COVERAGE_PATHS-coverage.xml}"  # set empty = project config applies
DEFAULT_LINT_CMD=""
[ "$PROJECT_DIR" = "$FABRIKANT_DIR" ] && DEFAULT_LINT_CMD='bash scripts/lint.sh --reports "$SONAR_LINT_DIR"'
LINT_CMD="${SONAR_LINT_CMD-$DEFAULT_LINT_CMD}"  # set empty = no analyzer reports
LINT_DIR="${SONAR_LINT_DIR:-.fabrikant/sonar/lint}"
# Report file (from SONAR_LINT_CMD) → scanner parameter that imports it.
LINT_REPORTS=(
  "ruff.json=sonar.python.ruff.reportPaths"
  "bandit.json=sonar.python.bandit.reportPaths"
  "pyright-sonar.json=sonar.externalIssuesReportPaths"
)
SCANNER="${SONAR_SCANNER:-}"
read -ra EXTRA_SCANNER_ARGS <<< "${SONAR_SCANNER_ARGS:-}"
# Token file: shareable via SONAR_TOKEN_FILE (e.g. main repo instead of worktree,
# so that worktree runs reuse the same token and do not create a flood of tokens).
TOKEN_FILE="${SONAR_TOKEN_FILE:-$FABRIKANT_DIR/state/sonar-token.txt}"
READY_TIMEOUT="${SONAR_READY_TIMEOUT:-180}"
BRANCH="${SONAR_BRANCH:-}"
PR="${SONAR_PR:-}"
AUTO=auto
REPORT_FILE="${SONAR_REPORT_FILE:-}"
COMMENT_FILE="${SONAR_COMMENT_FILE:-}"
REPORT_METRICS="new_coverage,new_duplicated_lines_density,new_violations,new_security_hotspots,coverage"
REPORT_MAX_ISSUES=100
# Unresolved spec placeholders (`{pr_number}` in a cron run) mean "unset".
case "$BRANCH" in "{"*) BRANCH="" ;; esac
case "$PR" in "{"*) PR="" ;; esac

# PR mode (advisory, CLI): --pr <n> [--post] [--base <branch>] — own key, report instead of gate.
# Independent of SONAR_PR (native PR analysis): CLI mode scans under <PROJECT_KEY>:pr-<n>.
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

# PR mode: logs to stderr (stdout carries only the PR report); default: stdout (as for main).
log() {
  if [ -n "$CLI_PR" ]; then
    echo "[sonar-scan] $*" >&2
  else
    echo "[sonar-scan] $*"
  fi
}

# 1) Start SonarQube (if not up)
# (api/system/status needs no auth — api/system/health always answers 401 without a token.)
if ! curl -sf -o /dev/null --max-time 3 "$HOST_URL/api/system/status"; then
  log "SonarQube nicht erreichbar → docker compose up -d ($COMPOSE_FILE)"
  if ! docker compose -f "$COMPOSE_FILE" up -d; then
    log "FEHLER: docker compose up fehlgeschlagen"
    exit "$EX_CONFIG"
  fi
fi

# 2) Token (cache it, otherwise create a new one).
#    Before the readiness check, so that the cached token also serves as the auth
#    source — the admin password changes after the first login and is
#    no longer known afterwards (and e.g. in worktrees).
mkdir -p "$(dirname "$TOKEN_FILE")"
if [ -s "$TOKEN_FILE" ]; then
  TOKEN="$(cat "$TOKEN_FILE")"
  log "Nutze gecachten Scanner-Token"
else
  log "Erzeuge Scanner-Token (Admin-Creds: $ADMIN_USER) …"
  rc=0
  resp="$(curl -sf --max-time 10 -u "$ADMIN_USER:$ADMIN_PASS" \
    -X POST "$HOST_URL/api/user_tokens/generate?name=fabrikant-scanner")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    # curl 22 = HTTP error (e.g. 401: admin password changed) → human; otherwise network/timeout.
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

# 3) Wait for readiness (health=GREEN, basic auth with scanner token).
#    401/403: token revoked or SonarQube reset — waiting does not heal that (→ environment).
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

# 3b) PR run: run worktrees are detached on origin/<repo.branch> — without a checkout the PR trigger
#     would scan main. Empty or starting with `{` (unresolved placeholder, cron) = no PR.
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

# Resolve the analysis target → SCAN_ARGS (pysonar parameters) + GATE_QUERY (filter for project_status).
# After the PR head checkout (3b, HEAD comparison) and before the expensive test run (call errors early).
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

# 4) Tests + coverage report (Cobertura XML) — without coverage data the quality gate fails on
#    new_coverage (default: >= 80 %). fabrikant default: perf tests excluded (as in CI), their
#    wall-clock budget breaks under coverage instrumentation.
cd "$PROJECT_DIR"
# PR mode (advisory): a fresh coverage report (younger than HEAD) is reused —
# the expensive test run is skipped, the scan only runs for the report. `git log` only in
# PR mode (condition): in foreign project dirs without a git repo it would otherwise fail.
if [ -n "$CLI_PR" ] && [ -f coverage.xml ] && [ "$(stat -c %Y coverage.xml)" -ge "$(git log -1 --format=%ct)" ]; then
  log "Nutze vorhandenen Coverage-Report (jünger als HEAD) — Tests übersprungen"
else
  log "Tests + Coverage-Report ($TEST_CMD) …"
  rc=0
  bash -c "$TEST_CMD" || rc=$?
  # pytest: 1 = tests red, 2 = collection error/aborted (code result); 3–5 = internal/usage error,
  # no tests (setup); pass signals (>128) through unchanged.
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

# No coverage file → Sonar reports 0 % only as an analysis warning; make it visible here. Not an error:
# a PR without an affected test package legitimately produces none.
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

# 4b) Analyzer reports (ruff/bandit/pyright) → external issues. Empty beforehand: no upload of stale reports.
LINT_ARGS=()
if [ -n "$LINT_CMD" ]; then
  rm -rf "$LINT_DIR"
  mkdir -p "$LINT_DIR"
  log "Analyzer-Reports ($LINT_CMD) …"
  rc=0
  SONAR_LINT_DIR="$LINT_DIR" bash -c "$LINT_CMD" || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "FEHLER: Analyzer-Reports fehlgeschlagen (exit $rc)"
    # 126/127 (tool missing) and signals unchanged; otherwise setup error — findings themselves are not an exit ≠ 0.
    if [ "$rc" -eq "$EX_NOEXEC" ] || [ "$rc" -eq "$EX_NOTFOUND" ] || [ "$rc" -gt "$EX_SIGNAL_BASE" ]; then exit "$rc"; fi
    exit "$EX_CONFIG"
  fi
  for entry in "${LINT_REPORTS[@]}"; do
    file="$LINT_DIR/${entry%%=*}"
    if [ -s "$file" ]; then LINT_ARGS+=("-D${entry#*=}=$file"); fi
  done
  log "Analyzer-Reports: ${#LINT_ARGS[@]} von ${#LINT_REPORTS[@]} gefunden"
fi

# 5) Scan (pysonar reads [tool.sonar] from pyproject.toml in PROJECT_DIR, if present).
#    Output is tee'd so that we can extract the CE task ID for step 6
#    (pysonar prints: …/api/ce/task?id=<uuid>).
export SONAR_HOST_URL="$HOST_URL"
export SONAR_TOKEN="$TOKEN"
log "Führe Scanner aus (projectKey=$PROJECT_KEY) …"
# Check the scanner beforehand: `uv run` reports a missing command with exit 2 — below that would look like a
# failed upload (75, transient) instead of an environment error (127, ADR 0035).
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
  # PR mode: logs to stderr, stdout carries only the PR report (sonar-pr-report.py).
  "${SCANNER_CMD[@]}" "${COVERAGE_ARGS[@]}" "${LINT_ARGS[@]}" "${EXTRA_SCANNER_ARGS[@]}" "${SCAN_ARGS[@]}" 2>&1 \
    | tee "$SCAN_LOG" >&2 || rc=$?
else
  "${SCANNER_CMD[@]}" "${COVERAGE_ARGS[@]}" "${LINT_ARGS[@]}" "${EXTRA_SCANNER_ARGS[@]}" "${SCAN_ARGS[@]}" 2>&1 \
    | tee "$SCAN_LOG" || rc=$?
fi
TASK_ID="$(grep -oE 'api/ce/task\?id=[a-f0-9-]+' "$SCAN_LOG" | tail -1 | cut -d= -f2 || true)"
SCANNER_RC=0
if [ "$rc" -ne 0 ] && [ -n "$TASK_ID" ]; then
  # Upload succeeded, the project's scan script then reports an error (e.g. tests red):
  # gate + report anyway (PR comment), exit 1 at the end instead of "transient".
  SCANNER_RC="$rc"
  log "WARNUNG: Scanner meldet exit $rc nach dem Upload — Analyse wird ausgewertet, Ergebnis rot"
elif [ "$rc" -ne 0 ]; then
  log "FEHLER: Scanner fehlgeschlagen (exit $rc)"
  # 126/127: scanner not executable (environment); signal: unknown; otherwise the upload to SonarQube fails.
  if [ "$rc" -eq "$EX_NOEXEC" ] || [ "$rc" -eq "$EX_NOTFOUND" ] || [ "$rc" -gt "$EX_SIGNAL_BASE" ]; then exit "$rc"; fi
  exit "$EX_TEMPFAIL"
fi

# 6) Check the quality gate.
#    IMPORTANT: first wait for CE task completion. Otherwise the first
#    gate check reads the STALE status of the PREVIOUS analysis (race: upload is
#    synchronous, processing asynchronous) → false 'ERROR' despite a green result.
#    After CE SUCCESS the gate status of the current analysis is definitive.
if [ -z "$TASK_ID" ]; then
  log "FEHLER: keine CE-Task-ID im pysonar-Output gefunden"
  exit "$EX_CONFIG"  # scanner output does not match the script
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
  exit "$EX_TEMPFAIL"  # FAILED/CANCELED/not retrievable: server side, no code result
fi

# PR mode (advisory, CLI): report instead of gate.
if [ -n "$CLI_PR" ]; then
  git fetch -q origin "$BASE" >&2 || log "WARN: git fetch origin $BASE fehlgeschlagen — nutze lokalen Stand"
  uv run python scripts/sonar-pr-report.py --pr "$CLI_PR" --project-key "$PROJECT_KEY" --base "$BASE" $POST
  exit 0
fi

# Gate status (definitive after CE completion; transient values: UNKNOWN/NONE).
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

# 6b) Result report (SONAR_REPORT_FILE, optionally SONAR_COMMENT_FILE) — before the gate evaluation, so that
#     a red gate also lands in the PR (loop `sonar-pr`). Rendering: scripts/sonar_report.py (no LLM).
#     Follow-up steps thus need no Sonar token.
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
  python3 "$FABRIKANT_DIR/scripts/sonar_report.py" "${report_args[@]}"
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

# 7) Clean up orphaned legacy PR projects (ADR 0053, advisory) — only in the main scan
#    (SONAR_PR empty), not in PR scans: delete `<PROJECT_KEY>:pr-<n>` projects from the time before
#    native PR analysis whose PR is merged/closed. The script is
#    fail-safe (undeterminable PR status → no deletion) and ends with exit 0 on errors;
#    the additional `if !` guard ensures that no failure breaks the successful
#    scan.
if [ -z "$PR" ]; then
  log "Räume verwaiste Sonar-PR-Projekte auf …"
  if ! uv run python scripts/sonar-pr-cleanup.py; then
    log "WARNUNG: Aufräumen der Sonar-PR-Projekte fehlgeschlagen (ignoriert)"
  fi
fi
