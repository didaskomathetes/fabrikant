#!/usr/bin/env bash
# Create or update a PR/issue comment — exactly one per marker — as the fabrikant GitHub App.
#
# Usage: gh-upsert-comment.sh <owner/repo> <number> <marker> <body-file>
#
# The marker (e.g. `<!-- fabrikant-sonar -->`) is prepended to the body. An existing
# bot comment that starts with it is replaced, otherwise a new one is created — re-runs per
# push update the same comment instead of spamming the PR.
#
# Env:
#   GH_ROLE  dev | reviewer (default reviewer) — token GITHUB_TOKEN or GITHUB_REVIEWER_TOKEN from
#            the project's token file (FABRIKANT_PROJECT_ENV_FILE, otherwise FABRIKANT_ENV_FILE; ADR 0010).
#            Keep it fresh: loop `token-refresh`.
#
# Exit: 0 ok · 78 EX_CONFIG (usage, token missing) · otherwise the exit code of `gh` (API error).
set -euo pipefail

EX_CONFIG=78

die() { echo "[gh-upsert-comment] ERROR: $*" >&2; exit "$EX_CONFIG"; }

[ "$#" -eq 4 ] || die "Usage: $0 <owner/repo> <number> <marker> <body-file>"
repo="$1" number="$2" marker="$3" body_file="$4"
[ -r "$body_file" ] || die "Body file not readable: $body_file"
case "$number" in ''|*[!0-9]*) die "not a number: $number" ;; esac

case "${GH_ROLE:-reviewer}" in
  dev) token_key=GITHUB_TOKEN ;;
  reviewer) token_key=GITHUB_REVIEWER_TOKEN ;;
  *) die "unknown GH_ROLE: $GH_ROLE (dev|reviewer)" ;;
esac
env_file="${FABRIKANT_PROJECT_ENV_FILE:-${FABRIKANT_ENV_FILE:-}}"
[ -r "$env_file" ] || die "Token file not readable: ${env_file:-<FABRIKANT_ENV_FILE not set>}"
GH_TOKEN="$(sed -n "s/^$token_key=[\"']\{0,1\}\([^\"']*\).*/\1/p" "$env_file" | tail -1)"
[ -n "$GH_TOKEN" ] || die "$token_key missing in $env_file"
export GH_TOKEN
unset GITHUB_TOKEN  # gh prefers GH_TOKEN, but do not pass a builder token through from the step env

body="$marker
$(cat "$body_file")"
# Only replace own comments — never a foreign one that quotes the marker. Own login as in
# pr-status.py: FABRIKANT_REVIEWER_LOGIN, otherwise `gh api user` (PAT); app tokens may not query
# /user → then any bot (the app is the only bot that posts with the marker).
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
