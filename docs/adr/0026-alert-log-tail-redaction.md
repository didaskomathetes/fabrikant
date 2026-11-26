# ADR 0026: Redaction of the alert `log_tail`

**Status:** Accepted (2026-10-20, merge #187; proposed 2026-10-20). Follow-up issue
#132 to ADR 0025
(there the current state "without redaction" remains as historical context).

## Decision

- `secrets.redact_text` masks pattern-based with `[REDACTED]`:
  bearer tokens, AWS keys (`AKIA…`/`ASIA…`), known API key prefixes
  (`sk-`, `ghp_`, `github_pat_`, `xox*-`), private key blocks and generic
  `key=`/`token=`/`password=`/`secret=` assignments.
- Single call point: `_notify` redacts the tail once; Slack, webhooks and
  alert issues/comments use the same value.
- Switch `config.ALERT_LOG_REDACTION` (default `True`).
- **Fail-safe:** if the redaction throws, the post goes out unredacted + a warning
  is logged — an alert is never lost.

## Trade-offs

Heuristic: false positives (too much masked) are accepted, false negatives
(unknown secret formats) remain possible — the ops-only visibility from
ADR 0025 still applies.
