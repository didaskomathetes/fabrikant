#!/usr/bin/env bash
# Statische Checks der Fabrik — eine Quelle für CI (.github/workflows/fabrikant-ci.yml), den Lint-Gate von
# develop/refactor (`lint_cmd` in projects/fabrikant.fabrikant.yaml) und die Sonar-Reports (scripts/sonar-scan.sh).
#
#   bash scripts/lint.sh                 Gate: ruff check + ruff format --check + pyright + bandit (ab medium).
#                                        Läuft alle Checks durch, Exit 1 wenn einer rot ist.
#   bash scripts/lint.sh --reports DIR   Sonar-Reports nach DIR (Funde sind kein Fehler, Exit 0):
#                                          ruff.json           → sonar.python.ruff.reportPaths
#                                          bandit.json         → sonar.python.bandit.reportPaths
#                                          pyright-sonar.json  → sonar.externalIssuesReportPaths
#                                        Exit 78 (EX_CONFIG), wenn ein Tool selbst scheitert (nicht: Funde).
#
# Konfiguration: [tool.ruff], [tool.pyright], [tool.bandit] in pyproject.toml.
set -euo pipefail

EX_FINDINGS=1
EX_CONFIG=78

cd "$(dirname "${BASH_SOURCE[0]}")/.."
RUFF_PATHS=(src tests scripts)
REPORT_RUFF_PATHS=(src tests)  # nur was Sonar indiziert (sources + tests), sonst „unresolved file“-Warnungen
BANDIT_PATHS=(src)
BANDIT=(uv run bandit -c pyproject.toml -q -r "${BANDIT_PATHS[@]}")

log() { echo "[lint] $*"; }

if [ "${1:-}" = "--reports" ]; then
  dir="${2:?Aufruf: lint.sh --reports DIR}"
  mkdir -p "$dir"
  failed=0
  # Exit 1 = Funde (erwartet); alles darüber = Tool-/Konfigurationsfehler.
  report() {
    local name="$1"; shift
    local rc=0
    "$@" || rc=$?
    if [ "$rc" -gt "$EX_FINDINGS" ]; then log "FEHLER: $name exit $rc"; failed=1; fi
  }
  report ruff uv run ruff check --output-format json --output-file "$dir/ruff.json" "${REPORT_RUFF_PATHS[@]}"
  report bandit "${BANDIT[@]}" -f json -o "$dir/bandit.json"
  report pyright sh -c 'uv run pyright --outputjson > "$1"' _ "$dir/pyright.json"
  # Konverter kennt keine „Funde“: jeder Exit ≠ 0 ist ein Fehler.
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
# Spec-Referenz muss zum Schema passen (Issue #131): scripts/generate-spec-reference.py --check
check "spec-reference aktuell" uv run python scripts/generate-spec-reference.py --check
if [ "${#failed[@]}" -gt 0 ]; then
  log "ROT: ${failed[*]}"
  exit "$EX_FINDINGS"
fi
log "alle Checks grün"
