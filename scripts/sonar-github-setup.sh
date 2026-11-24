#!/usr/bin/env bash
# Connects the local SonarQube (compose.yaml, community branch plugin) with GitHub so that
# PR analyses (`SONAR_PR=<nr> scripts/sonar-scan.sh`) decorate the PR: summary comment with
# quality gate, coverage and duplication plus a check run with annotations for new issues.
# Without checks permission: loop `sonar-pr` (docs/sonarqube.md).
#
# Idempotent: creates or updates the GitHub App configuration and binds the project.
#
# Prerequisite: a GitHub App (Settings → Developer settings → GitHub Apps → New), installed
# on the repo, with repository permissions:
#   Checks: Read & write · Pull requests: Read & write · Contents: Read-only · Metadata: Read-only
# The webhook may be disabled (SonarQube only talks outbound to the GitHub API).
#
# Env (required):
#   SONAR_GH_APP_ID          App ID
#   SONAR_GH_CLIENT_ID       Client ID
#   SONAR_GH_CLIENT_SECRET   Client secret
#   SONAR_GH_PRIVATE_KEY     path to the app's .pem file
# Env (optional):
#   SONAR_HOST_URL           (default: http://127.0.0.1:9000)
#   SONAR_ADMIN_USER         (default: admin)
#   SONAR_ADMIN_PASSWORD     (default: admin)
#   SONAR_PROJECT_KEY        (default: fabrikant:fabrikant)
#   SONAR_GH_REPO            owner/repo (default: from `gh repo view`)
#   SONAR_GH_ALM_KEY         name of the configuration in SonarQube (default: github)
set -euo pipefail

HOST_URL="${SONAR_HOST_URL:-http://127.0.0.1:9000}"
AUTH="${SONAR_ADMIN_USER:-admin}:${SONAR_ADMIN_PASSWORD:-admin}"
PROJECT_KEY="${SONAR_PROJECT_KEY:-fabrikant:fabrikant}"
ALM_KEY="${SONAR_GH_ALM_KEY:-github}"
GITHUB_API_URL="https://api.github.com/"

log() { echo "[sonar-github-setup] $*"; }
die() { log "ERROR: $*"; exit 78; }  # EX_CONFIG

for var in SONAR_GH_APP_ID SONAR_GH_CLIENT_ID SONAR_GH_CLIENT_SECRET SONAR_GH_PRIVATE_KEY; do
  [ -n "${!var:-}" ] || die "$var not set"
done
[ -r "$SONAR_GH_PRIVATE_KEY" ] || die "Private key not readable: $SONAR_GH_PRIVATE_KEY"
REPO="${SONAR_GH_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"

# POST to the SonarQube web API; print the API's error text instead of just the HTTP code.
api() {
  local endpoint="$1"; shift
  local out
  if ! out="$(curl -sS --fail-with-body -u "$AUTH" -X POST "$HOST_URL/api/$endpoint" "$@")"; then
    die "$endpoint: $out"
  fi
}

params=(
  --data-urlencode "key=$ALM_KEY"
  --data-urlencode "url=$GITHUB_API_URL"
  --data-urlencode "appId=$SONAR_GH_APP_ID"
  --data-urlencode "clientId=$SONAR_GH_CLIENT_ID"
  --data-urlencode "clientSecret=$SONAR_GH_CLIENT_SECRET"
  --data-urlencode "privateKey@$SONAR_GH_PRIVATE_KEY"
)
if curl -sS -u "$AUTH" "$HOST_URL/api/alm_settings/list_definitions" | grep -q "\"key\":\"$ALM_KEY\""; then
  log "Aktualisiere GitHub-Konfiguration '$ALM_KEY'"
  api alm_settings/update_github "${params[@]}"
else
  log "Lege GitHub-Konfiguration '$ALM_KEY' an"
  api alm_settings/create_github "${params[@]}"
fi

# The project must exist before it can be bound (otherwise the first scan creates it).
if ! curl -sS -u "$AUTH" "$HOST_URL/api/projects/search?projects=$PROJECT_KEY" | grep -q "\"key\":\"$PROJECT_KEY\""; then
  log "Creating project $PROJECT_KEY"
  api projects/create --data-urlencode "project=$PROJECT_KEY" --data-urlencode "name=Fabrikant" \
    --data-urlencode "mainBranch=main"
fi

# monorepo=true: fabrikant is a subfolder of $REPO (several Sonar projects per repo possible).
log "Binding $PROJECT_KEY to $REPO"
api alm_settings/set_github_binding --data-urlencode "almSetting=$ALM_KEY" \
  --data-urlencode "project=$PROJECT_KEY" --data-urlencode "repository=$REPO" \
  --data-urlencode "monorepo=true" --data-urlencode "summaryCommentEnabled=true"

if ! out="$(curl -sS --fail-with-body -u "$AUTH" -G "$HOST_URL/api/alm_settings/validate" \
    --data-urlencode "key=$ALM_KEY")"; then
  die "Validation failed (app installed? permissions?): $out"
fi
log "OK — GitHub configuration valid; PR analyses now decorate PRs in $REPO"
