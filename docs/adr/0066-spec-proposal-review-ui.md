# ADR 0066: Spec-Proposal Review in the Web UI

**Status:** Proposed (2026-11-23)
**Betrifft:** ADR 0064 (MCP server, spec proposals), ADR 0030 (auth/roles/CSRF), ADR 0055 (UI event handling)

## Context

An MCP agent stores spec-change proposals (`propose_spec_change`, ADR 0064). Today a human can review
them only on the CLI (`fabrikant proposals show|apply|reject`), where `show` prints the raw patch. The
operator already lives in the web UI and sees pending run approvals there; a proposal needs the same
visibility, and a reviewer needs to see *what would change*, not the patch.

## Decision

- **Server-side diff.** `fabrikant.proposal_diff` runs the patch through the same dry run as the CLI
  (`save_spec(..., dry_run=True)`) and compares the validated result with the spec on disk: per loop
  added / removed / changed (before/after YAML plus a unified line diff) and changed top-level
  `description` / `max_parallel`. Values of secret-looking keys are masked before rendering
  (`secrets.redact_secret_keys`, shared with the MCP server), so a secret never reaches the browser or the
  diff lines. The proposal's raw `patch` is never sent.
- **Stale detection.** `stale` is true when the current spec hash differs from the proposal's `base_hash`.
  The diff is still shown (against today's spec) but apply is refused.
- **Endpoints** (`/api/spec-proposals`): `GET ?project=&status=` (metadata list, default pending),
  `GET /<id>` (with diff), `POST /<id>/apply`, `POST /<id>/reject`. Reads need `viewer`, decisions
  `operator` (the role that may edit specs via `PUT /api/projects/<p>/spec`), with the usual session
  CSRF rules. Apply/reject call `spec_proposals.apply/reject`, i.e. the CLI code path (atomic write,
  `.bak`, lock check); stale or already decided proposals answer `409`. The audit actor is `ui:<user>`.
- **UI.** The project row shows a badge with the number of pending proposals (also in
  `/api/projects` as `pending_proposals`, metadata only), an expandable list, and a modal with the
  summary line, collapsible per-loop red/green diffs, a stale warning that disables Apply, and
  confirmed Apply/Reject buttons. Viewers see the diff without buttons.

## Consequences

- No new write path: the UI cannot apply anything the CLI could not.
- The diff compares the expanded (template-resolved) loops, so a changed template shows up in every loop using it.
- Decided proposals stay on disk as audit records and are no longer diffed.
