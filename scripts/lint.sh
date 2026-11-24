#!/usr/bin/env bash
# Static checks of the factory — one source for CI (.github/workflows/fabrikant-ci.yml), the lint gate of
# develop/refactor (`lint_cmd` in projects/fabrikant.fabrikant.yaml) and the Sonar reports (scripts/sonar-scan.sh).
#
#   bash scripts/lint.sh                 Gate: ruff check + ruff format --check + pyright + bandit (medium and up).
#                                        Runs all checks, exit 1 if any is red.
#   bash scripts/lint.sh --reports DIR   Sonar reports to DIR (findings are not an error, exit 0):
#                                          ruff.json           → sonar.python.ruff.reportPaths
#                                          bandit.json         → sonar.python.bandit.reportPaths
#                                          pyright-sonar.json  → sonar.externalIssuesReportPaths
#                                        Exit 78 (EX_CONFIG) if a tool itself fails (not: findings).
#
# Configuration: [tool.ruff], [tool.pyright], [tool.bandit] in pyproject.toml.
set -euo pipefail

EX_FINDINGS=1
EX_CONFIG=78

cd "$(dirname "${BASH_SOURCE[0]}")/.."
RUFF_PATHS=(src tests scripts)
REPORT_RUFF_PATHS=(src tests)  # only what Sonar indexes (sources + tests), otherwise "unresolved file" warnings
BANDIT_PATHS=(src)
BANDIT=(uv run bandit -c pyproject.toml -q -r "${BANDIT_PATHS[@]}")

log() { echo "[lint] $*"; }

if [ "${1:-}" = "--reports" ]; then
  dir="${2:?Aufruf: lint.sh --reports DIR}"
  mkdir -p "$dir"
  failed=0
  # Exit 1 = findings (expected); anything above = tool/configuration error.
  report() {
    local name="$1"; shift
    local rc=0
    "$@" || rc=$?
    if [ "$rc" -gt "$EX_FINDINGS" ]; then log "FEHLER: $name exit $rc"; failed=1; fi
  }
  report ruff uv run ruff check --output-format json --output-file "$dir/ruff.json" "${REPORT_RUFF_PATHS[@]}"
  report bandit "${BANDIT[@]}" -f json -o "$dir/bandit.json"
  report pyright sh -c 'uv run pyright --outputjson > "$1"' _ "$dir/pyright.json"
  # The converter has no notion of "findings": every exit ≠ 0 is an error.
  if [ "$failed" -eq 0 ] && ! python3 scripts/pyright_sonar.py "$dir/pyright.json" "$dir/pyright-sonar.json"; then
    log "FEHLER: pyright_sonar"; failed=1
  fi
  if [ "$failed" -ne 0 ]; then exit "$EX_CONFIG"; fi
  log "Reports in $dir"
  exit 0
fi

failed=()
check() {
  local name="$1"; shift
  log "$name …"
  "$@" || failed+=("$name")
}
check "ruff check" uv run ruff check "${RUFF_PATHS[@]}"
check "ruff format" uv run ruff format --check "${RUFF_PATHS[@]}"
check "pyright" uv run pyright
check "bandit" "${BANDIT[@]}" --severity-level medium
# The spec reference must match the schema (issue #131): scripts/generate-spec-reference.py --check
check "spec-reference aktuell" uv run python scripts/generate-spec-reference.py --check
if [ "${#failed[@]}" -gt 0 ]; then
  log "ROT: ${failed[*]}"
  exit "$EX_FINDINGS"
fi
log "alle Checks grün"
