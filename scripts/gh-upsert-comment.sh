#!/usr/bin/env bash
# PR-/Issue-Kommentar anlegen oder aktualisieren — genau einer pro Marker — als fabrikant-GitHub-App.
#
# Usage: gh-upsert-comment.sh <owner/repo> <nummer> <marker> <body-datei>
#
# Der Marker (z. B. `<!-- fabrikant-sonar -->`) wird dem Body vorangestellt. Ein vorhandener
# Bot-Kommentar, der mit ihm beginnt, wird ersetzt, sonst wird ein neuer angelegt — Re-Runs pro
# Push aktualisieren denselben Kommentar statt den PR zuzuspammen.
#
# Env:
#   GH_ROLE  dev | reviewer (Default reviewer) — Token GITHUB_TOKEN bzw. GITHUB_REVIEWER_TOKEN aus
#            der Token-Datei des Projekts (FABRIKANT_PROJECT_ENV_FILE, sonst FABRIKANT_ENV_FILE; ADR 0010).
#            Frisch halten: Loop `token-refresh`.
#
# Exit: 0 ok · 78 EX_CONFIG (Aufruf, Token fehlt) · sonst Exit-Code von `gh` (API-Fehler).
set -euo pipefail

EX_CONFIG=78

die() { echo "[gh-upsert-comment] FEHLER: $*" >&2; exit "$EX_CONFIG"; }

[ "$#" -eq 4 ] || die "Usage: $0 <owner/repo> <nummer> <marker> <body-datei>"
repo="$1" number="$2" marker="$3" body_file="$4"
[ -r "$body_file" ] || die "Body-Datei nicht lesbar: $body_file"
case "$number" in ''|*[!0-9]*) die "keine Nummer: $number" ;; esac

case "${GH_ROLE:-reviewer}" in
  dev) token_key=GITHUB_TOKEN ;;
  reviewer) token_key=GITHUB_REVIEWER_TOKEN ;;
  *) die "unbekannte GH_ROLE: $GH_ROLE (dev|reviewer)" ;;
esac
env_file="${FABRIKANT_PROJECT_ENV_FILE:-${FABRIKANT_ENV_FILE:-}}"
[ -r "$env_file" ] || die "Token-Datei nicht lesbar: ${env_file:-<FABRIKANT_ENV_FILE nicht gesetzt>}"
GH_TOKEN="$(sed -n "s/^$token_key=[\"']\{0,1\}\([^\"']*\).*/\1/p" "$env_file" | tail -1)"
[ -n "$GH_TOKEN" ] || die "$token_key fehlt in $env_file"
export GH_TOKEN
unset GITHUB_TOKEN  # gh bevorzugt GH_TOKEN, aber kein Builder-Token aus dem Step-Env durchreichen

body="$marker
$(cat "$body_file")"
# Nur eigene Kommentare ersetzen — nie einen fremden, der den Marker zitiert. Eigener Login wie in
# pr-status.py: FABRIKANT_REVIEWER_LOGIN, sonst `gh api user` (PAT); App-Tokens dürfen /user nicht
# abfragen → dann jeder Bot (die App ist der einzige Bot, der mit dem Marker postet).
own_login="${FABRIKANT_REVIEWER_LOGIN:-$(gh api user --jq .login 2>/dev/null || true)}"
if [ -n "$own_login" ]; then
  author_filter=".user.login == \"$own_login\""
else
  author_filter='.user.type == "Bot"'
fi
existing="$(gh api --paginate "repos/$repo/issues/$number/comments" \
  --jq ".[] | select($author_filter and (.body | startswith(\"$marker\"))) | .id" | tail -1)"
if [ -n "$existing" ]; then
  gh api -X PATCH "repos/$repo/issues/comments/$existing" -f body="$body" --jq .html_url
else
  gh api "repos/$repo/issues/$number/comments" -f body="$body" --jq .html_url
fi
