# Triggers, pause/disable and alerts

Trigger types (`config.TRIGGER_TYPES`): `manual`, `cron`, `on_file`, `on_issue`, `on_pr`, `on_webhook`,
`on_alert`, `on_slack`.

- **manual** — UI button, CLI, API, chat
- **cron** — 5 fields (`min hour dom mon dow`, `*`, `*/n`, `a-b`, lists). `on_start: true` additionally fires
  once after server start (or on the first tick after a resume) — e.g. for `token-refresh`, so that after a
  server pause no loop starts with an expired 1h token. Until this run is done (max. 2 min), the project's other
  triggers (cron, file, issue and PR polls) wait
- **on_file** — fires when a file in the glob (relative to `root`) changes and has been stable for `debounce`
  seconds
- **on_issue** — polls GitHub (`repo` from spec or trigger, optional `labels`), starts the loop for every new
  issue. With `reconcile: true` (ADR 0014) it instead fires for every open issue for which the loop has never
  had a run — survives restarts; max. `max_per_poll` (default 2) runs per poll, oldest first. Optional
  `priority_labels` (ADR 0021, e.g. `[p0, p1, p2, p3]`): issues with a higher priority label first, unlabeled
  last. `skip_blocked: true` (ADR 0031, only with `reconcile`): issues with unresolved GitHub "blocked by"
  dependencies don't start, but on the first poll after the last blocker was closed as `completed`
  (`duplicate`/`not_planned` keep blocking). `exclude_labels` (also on **on_pr**, ADR 0059, e.g.
  `[needs human]`): items with one of these labels do not start; once the label is removed they fire again
- **on_pr** — polls GitHub PRs (`repo` from spec or trigger, optional `labels` (AND semantics), `poll` in
  seconds, `states: [open|closed]` (default `[open]`)), starts the loop for new/updated PRs (updated_at change)
  and on state changes (e.g. open→closed if `closed` is in `states`). Dedup: a PR fires only on change — polling
  the same PR again doesn't trigger a second run. Variables: `{pr_number}`, `{pr_title}`, `{pr_head_ref}`,
  `{pr_body}`. `reconcile: true` (ADR 0011): also fires for existing PRs, but only if no previous `on_pr` run
  exists (status irrelevant; restart-proof; reconciling controller). `reconcile: head` (ADR 0023): like `true`,
  but per head SHA (`on_pr:#N@<sha>`) — every new push fires exactly once, a discarded trigger (lock busy,
  restart) is caught up on the next poll. `reconcile: updated`: like `head`, but per PR change (`updated_at`,
  `on_pr:#N@u<stamp>`) — pushes, comments, reviews and label changes each fire exactly once, also when they
  happened during a restart (e.g. a loop answering review comments). The loop's own PR actions (reply comment,
  push, label) also bump `updated_at` and fire it once more: it needs a gate step that ends without touching the
  PR when there is nothing new (e.g. `::fabrikant-done::`, see runs.md), otherwise it retriggers itself endlessly.
  Additional variable: `{pr_head_sha}`
- **on_webhook** — `POST /api/webhook/<project>/<loop>` (ADR 0027). Auth via HMAC-SHA256 of the raw body in the
  header `X-Fabrikant-Signature: sha256=<hex>`; wrong/missing → `401`. Replay protection via
  `X-Fabrikant-Timestamp`/`X-Fabrikant-Nonce` (ADR 0058). Payload→variables via `map` (dot paths; invalid JSON
  `400`, missing field `422`). CORS only for `FABRIKANT_WEBHOOK_ORIGIN` (tunnel origin, never `*`); over this
  host only the webhook route is reachable.
  ```yaml
  triggers:
    - type: on_webhook
      secret_env: MYAPP_HOOK_SECRET   # env name, never the value
      map: {issue_number: issue.number, title: issue.title}
  ```
- **on_alert** — `POST /api/alert/<project>/<loop>` (ADR 0045): Sentry/CloudWatch intake. Auth as with
  `on_webhook` (HMAC in `X-Fabrikant-Signature`; Sentry additionally `Sentry-Hook-Signature`), otherwise `401`.
  Variables: `alert_source|title|message|level|service|url|fingerprint`. CloudWatch state ≠ `ALARM` →
  `200 ignored`, no run. Reachable via the tunnel host.
- **on_slack** — polls Slack channels (ADR 0040) and starts exactly one run per new message matching the pattern
  (dedup via `ts` in `state/slack/`). Variables: `{alert_text}`, `{alert_channel}`, `{alert_ts}`. `alert_text` is
  untrusted (any Slack user in the channel can set it): in `llm` steps only via `untrusted: [alert_text]`, in
  `shell` steps only via `env:` — never inline in `cmd` (ADR 0008). Token only via an env variable
  (`token_env`), never in the spec; only channels on the `channels` whitelist are read.
  ```yaml
  triggers:
    - type: on_slack
      token_env: SLACK_BOT_TOKEN      # env name, never the value
      channels: [C0123ALERTS]         # whitelist (channel/DM IDs)
      pattern: "^(ALERT|FIRING)"      # optional, regex
      poll: 60
      max_pages: 20                   # optional, history pages (of 100) per channel and poll
      max_alert_chars: 4000           # optional, longer alert_text is truncated (marker with the character count)
  ```
  If `max_pages` is reached, the poll processes the newest messages and catches up on the older gap in the
  following polls (warning in the log) — no alert is lost, only the order across polls is then not
  chronological. `pattern` always checks the full text, truncation happens only afterwards.

| Trigger | Source | Mechanism | Auth |
|---|---|---|---|
| `on_issue` | GitHub issues | polling | GitHub token |
| `on_webhook` | HTTP sender | push | HMAC-SHA256 |
| `on_alert` | Sentry / CloudWatch | push | HMAC-SHA256 |
| `on_slack` | Slack channels/DMs | polling | bot token (env) |

## Pause / Disable (ADR 0019)

State per project and loop: `active` | `paused` (triggers don't fire, manual runs allowed) | `disabled`
(additionally no manual runs). Project state beats loop state; running runs finish. Optional `until` (ISO 8601,
without a time zone = UTC) → automatic resume. Persisted in `state/run-states.json` (not in the spec); every
change is in the audit log `state/run-state-changes.jsonl` (who, when, from, to, until).

```bash
fabrikant pause <project> [loop] [--until 2026-01-31T18:00]
fabrikant disable <project> [loop]
fabrikant resume <project> [loop]
```

Chat: `pause|resume|disable <project> [loop]`; UI: buttons per project/loop; API:
`POST /api/projects/<p>/state`. An emergency stop for all projects is not part of v1. Access: role `operator`
([auth](auth.md)).

## Incident round trip

Alert → triage → hotfix → verify → PR (ADR 0045). Further intake sources are tracked as issues (#33 `on_slack`
shipped as ADR 0040; #34 `on_datadog` is not implemented); the ACP backend status is in ADR 0044 (watching).

```yaml
loops:
  triage:
    worktree: false
    triggers:
      - {type: on_alert, source: sentry, secret_env: SENTRY_HOOK_SECRET}  # or cloudwatch
    steps:
      - name: triage            # read-only: classification only, alert fields untrusted
        type: llm
        prompt: "Classify the alert {alert_title}: {alert_message}"
        untrusted: [alert_title, alert_message]
        schema:
          category: {type: string, enum: [bug, infra, noise, unknown], default: unknown}
      - {name: route, type: alert_route, routes: {bug: hotfix}}   # unknown/noise → no hotfix
  hotfix:                       # runs as its own run with alert_* + category
    steps: [agent-fix, verify, pr]
```

Sources: **Sentry** — internal integration/webhook to the URL, client secret as `secret_env`. **CloudWatch** —
alarm → SNS → relay (Lambda/API Gateway) that signs the body with HMAC (`X-Fabrikant-Signature: sha256=<hex>`)
and forwards it; the SNS envelope is unpacked. The hotfix run is subject to the normal tier/approval gates.

## Alerts (`notify`)

After every run whose status is in `events`:

- **slack** — Slack webhook URL (string or `{url: ...}`)
- **webhooks** — arbitrary HTTP POST targets (Datadog HTTP intake, CloudWatch, custom endpoints); payload keys
  (German, kept for compatibility): `projekt, loop, status, lauf, trigger, message, log_tail`; on `run_failed`
  additionally `failure_kind` (also in the text and issue title; `environment` highlighted, ADR 0035)
- **github_issue** — on `run_failed`, create an issue in the `github` repo (title + log tail + label).
  **Dedup:** the same failure pattern (project/loop + failed step + error text without run/session IDs, i.e.
  UUIDs and hex tokens with a-f; decimal numbers — even long ones like timestamps — are kept) → as long as the
  issue is open, it is commented on instead of creating a new one. The lookup paginates over all open issues with
  the `issue_labels` (AND-combined — after a label change old issues no longer match). Tuning constants in
  `config.py`: `ALERT_OPEN_ISSUES_PAGE` (max. 100), `ALERT_LOOKUP_MAX_PAGES`, `ALERT_FP_LEN`,
  `ALERT_REASON_CAP` (details: ADR 0025)
- **log_tail policy:** known secrets (env/token files) are already masked in the log; in addition
  `secrets.redact_text` redacts bearer/API/AWS keys, private key blocks and `key=`/`token=` assignments to
  `[REDACTED]` (switch `config.ALERT_LOG_REDACTION`, on by default; on a redaction error fail-safe: unredacted +
  warning). Heuristic — still keep the targets restricted (ADR 0025, ADR 0026)

Test: `fabrikant alert test [--project X]` or `alert test` in the chat.
