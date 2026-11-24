#!/usr/bin/env bash
# Verbindet die lokale SonarQube (compose.yaml, Community-Branch-Plugin) mit GitHub, damit
# PR-Analysen (`SONAR_PR=<nr> scripts/sonar-scan.sh`) den PR dekorieren: Summary-Kommentar mit
# Quality Gate, Coverage und Duplication sowie ein Check Run mit Annotationen zu neuen Issues.
# Ohne Checks-Permission: Loop `sonar-pr` (docs/sonarqube.md).
#
# Idempotent: legt die GitHub-App-Konfiguration an bzw. aktualisiert sie und bindet das Projekt.
#
# Voraussetzung: eine GitHub-App (Settings → Developer settings → GitHub Apps → New), installiert
# auf dem Repo, mit Repository-Permissions:
#   Checks: Read & write · Pull requests: Read & write · Contents: Read-only · Metadata: Read-only
# Webhook darf deaktiviert sein (SonarQube spricht nur ausgehend mit der GitHub-API).
#
# Env (Pflicht):
#   SONAR_GH_APP_ID          App ID
#   SONAR_GH_CLIENT_ID       Client ID
#   SONAR_GH_CLIENT_SECRET   Client secret
#   SONAR_GH_PRIVATE_KEY     Pfad zur .pem-Datei der App
# Env (optional):
#   SONAR_HOST_URL           (Default: http://127.0.0.1:9000)
#   SONAR_ADMIN_USER         (Default: admin)
#   SONAR_ADMIN_PASSWORD     (Default: admin)
#   SONAR_PROJECT_KEY        (Default: fabrik:fabrik)
#   SONAR_GH_REPO            owner/repo (Default: aus `gh repo view`)
#   SONAR_GH_ALM_KEY         Name der Konfiguration in SonarQube (Default: github)
set -euo pipefail

HOST_URL="${SONAR_HOST_URL:-http://127.0.0.1:9000}"
AUTH="${SONAR_ADMIN_USER:-admin}:${SONAR_ADMIN_PASSWORD:-admin}"
PROJECT_KEY="${SONAR_PROJECT_KEY:-fabrik:fabrik}"
ALM_KEY="${SONAR_GH_ALM_KEY:-github}"
GITHUB_API_URL="https://api.github.com/"

log() { echo "[sonar-github-setup] $*"; }
die() { log "FEHLER: $*"; exit 78; }  # EX_CONFIG

for var in SONAR_GH_APP_ID SONAR_GH_CLIENT_ID SONAR_GH_CLIENT_SECRET SONAR_GH_PRIVATE_KEY; do
  [ -n "${!var:-}" ] || die "$var nicht gesetzt"
done
[ -r "$SONAR_GH_PRIVATE_KEY" ] || die "Private Key nicht lesbar: $SONAR_GH_PRIVATE_KEY"
REPO="${SONAR_GH_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"

# POST an die SonarQube-Web-API; Fehlertext der API ausgeben statt nur des HTTP-Codes.
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

# Projekt muss existieren, bevor es gebunden werden kann (erster Scan legt es sonst erst an).
if ! curl -sS -u "$AUTH" "$HOST_URL/api/projects/search?projects=$PROJECT_KEY" | grep -q "\"key\":\"$PROJECT_KEY\""; then
  log "Lege Projekt $PROJECT_KEY an"
  api projects/create --data-urlencode "project=$PROJECT_KEY" --data-urlencode "name=Fabrik" \
    --data-urlencode "mainBranch=main"
fi

# monorepo=true: fabrik ist ein Unterordner von $REPO (mehrere Sonar-Projekte pro Repo möglich).
log "Binde $PROJECT_KEY an $REPO"
api alm_settings/set_github_binding --data-urlencode "almSetting=$ALM_KEY" \
  --data-urlencode "project=$PROJECT_KEY" --data-urlencode "repository=$REPO" \
  --data-urlencode "monorepo=true" --data-urlencode "summaryCommentEnabled=true"

if ! out="$(curl -sS --fail-with-body -u "$AUTH" -G "$HOST_URL/api/alm_settings/validate" \
    --data-urlencode "key=$ALM_KEY")"; then
  die "Validierung fehlgeschlagen (App installiert? Permissions?): $out"
fi
log "OK — GitHub-Konfiguration gültig; PR-Analysen dekorieren jetzt PRs in $REPO"
