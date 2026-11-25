#!/usr/bin/env python3
"""pr-status — deterministic PR decisions for review-pr/pr-reply (ADR 0024).

The loop agents should not assemble reviews themselves nor derive merge approval
or "new comments" from raw JSON (LLM guessing = self-loops,
double posts, wrong merges). This script encapsulates that via `gh`:

  post-review <num> --repo R --payload state/review-<num>.json [--comment-only] [--sha SHA]
      Post one GitHub review (summary + inline comments) — only for the
      current head SHA, idempotent per SHA, at most MAX_INLINE inline comments.
      Optional `"refs"` in the payload (as for post-replies, checked live) → section "Referenced issues";
      free `#N` without backing are defused. Without a marker in the body: `"verdict": "approved" |
      "changes_requested"` + `--sha` (reviewed commit) → the host builds the marker; fabrikant markers in
      the body (forged by the agent) are stripped.
      `--comment-only`: always COMMENT instead of APPROVE/REQUEST_CHANGES — for repos in
      which the verdict stays advisory and humans approve (team repos with CODEOWNERS).
      Inline lines outside the diff hunks are moved into the summary beforehand;
      422 from GitHub → fallback without any inline comments.
  review-check <num> --repo R [--sha SHA]
      Check AFTER the review agent: has the reviewer account posted a marker review for SHA (default:
      current head)? An agent can report "done" without having posted.
      → {"reviewed": bool, "reason": "...", "head": "<sha>"}; exit 1 if not reviewed.
      If the head has moved on since SHA, that counts as ok (the agent then deliberately posts nothing;
      the next run reviews the new head).
  merge-check <num> --repo R
      → {"action": "merge" | "resolve_conflicts" | "wait", "reason": "..."}
      (mergeStateStatus mapping is explicit: MERGE_STATES / REBASE_STATES / WAIT_STATES).
      If the PR adds an ADR with a number already taken on the base → "wait"
      (ADR 0032; repair: `scripts/check-adr-numbers.py fix`). Label
      'needs approval' → "wait": such PRs are merged only by the approval loop (ADR 0011).
      Label `needs human` (`--human-label`) → "wait": escalated (ADR 0059).
  sync-base <num> --repo R [--conflict-label L]
      Keep the PR up to date with its base (no merge, no review): if the branch is behind, GitHub
      updates it via `update-branch` (the merge commit is signed by GitHub); on a conflict the PR
      gets the label L (default `fabrikant-conflict`), which an agent loop resolves; once it is
      up to date again, the label is removed.
      → {"action": "updated" | "up_to_date" | "conflict" | "pending", "reason": "...", "head": "<sha>"}
  new-comments <num> --repo R [--state state/replied-comments.json] [--ignore-bots [--keep-bot LOGIN]...]
      → [{"key", "kind", "id", "author", "created", "body", ...}] (chronological)
      Answered = the key is listed in a reply marker `re=` on the PR (stateless: survives
      throwaway worktrees and the sandbox) or, if given, in the old state file.
      `--keep-bot` is repeatable (e.g. QA reviewer app + Codex). Receipts of machine reviewers
      (BOT_RECEIPT_MARKERS: summary without findings of its own) never count — their findings are inline.
  reply-context <num> --repo R --out DIR [--head-prefix P] [--keep-bot LOGIN]... [--max-comments N] [--max-rounds N]
      Gate of the pr-reply loop (host, before the agent): new comments (at most N, humans before bots) to
      DIR/comments.json, PR state + fix rounds to DIR/context.json.
      → {"action": "reply" | "skip", "reason": "...", ...}; escalated PRs (`--human-label`) → skip.
  post-replies <num> --repo R --dir DIR [--commit SHA]
      Post the agent's reply drafts (DIR/replies.json) (host, after the push): only for
      keys from DIR/comments.json, thread/URL from the same file (never from the agent), `{commit}` =
      new commit of this run, marker `re=<keys>` appended, idempotent via the markers.
      A draft with `"escalate": "blocked" | "scope"` escalates (ADR 0059): escalation marker
      on the comment, then the host sets the label.
      `"refs": [{"issue": N, "relation": "covered_by"|"duplicate_of"|"blocked_by", "why": "…"}]`: references to
      existing issues — resolved and checked live (exists, not a PR, not the PR's own issue, matching
      state), rendered with their title; deferrals are only proposals (the thread stays open). Invalid
      references are dropped (warning `::fabrikant-warning::` on stderr, thread stays open). Free `#N`
      without backing are defused to code spans. `"follow_up": {"title", "body"}`: the host creates an
      issue with the label `fabrikant-followup` (at most 2 per PR, idempotent via marker) and links it.
      → {"posted": [...], "skipped": [...], "unanswered": [...], "followups": {...}?, "escalated": "<kind>"?};
      exit 1 on invalid drafts.
  escalation-check <num> --repo R [--stall-rounds N] [--max-rounds N] [--escalate] [--human-label L]
      Deterministic escalation (ADR 0059, no LLM): `stalled` = the last N verdicts of the
      reviewer are `changes_requested` without a non-merge commit in between;
      `exhausted` = `--max-rounds` fix commits used up and still `changes_requested` at the head.
      Counting starts at the last escalation comment: every resume grants a new budget.
      → {"action": "escalate" | "continue", "kind": "...", "reason": "..."}; `--escalate` then
      sets label + marker comment (see `escalate`).
  escalate <num> --repo R --kind K --why TEXT [--human-label L] [--builder]
      Escalation to a human (ADR 0059): comment with marker + label L (default
      `needs human`). Idempotent: if the item already has the label, nothing happens.
      `--builder` (builder draft, ADR 0059 §3): `--kind` only from BUILDER_ESCALATION_KINDS
      (`blocked`/`scope`) — only the host may set `stalled`/`exhausted`/`access`.
  issue-candidates <num> --repo R --out FILE [--closed-days N] [--excerpt N]
      Candidate list for agents (no GitHub access in the sandbox): open issues + issues closed in the
      last N days (no PRs) with title, state, labels, text excerpt → FILE. Untrusted data.
  threads <num> --repo R
      → open (unresolved) review threads
  resolve-thread <num> --repo R --comment-id <id>
      Mark the review thread containing this inline comment as resolved.

Reviewer account: `FABRIKANT_REVIEWER_LOGIN` (required for GitHub App tokens — they may not
query `/user`), otherwise `gh api user`. Bot logins are normalized
(`app/<name>` from gh JSON ≡ `<name>[bot]` from REST ≡ `<name>`).

Non-fatal problems are reported on stderr as `::fabrikant-warning::<text>` (WARNING_MARKER), so
stdout stays parseable JSON. Network blips of `gh` are retried (GH_RETRY_DELAYS_S); if a network
error persists, the script exits with 75 (EX_TEMPFAIL → failure class `transient`, run retry).

Markers (HTML comments, invisible in the rendered text):
  <!-- fabrikant-review verdict=approved|changes_requested sha=<40 hex> -->
      Verdict in the review body. Valid only from the reviewer account (`gh api user`) and
      only for the current head SHA — new commits need a new review.
      Needed because GitHub forbids approve/request-changes on one's own PRs.
  <!-- fabrikant-reply re=<key>[,<key>…] -->   (old form: <!-- fabrikant-reply -->)
      Reply of the pr-reply loop. Such comments are never "new" (no self-loop); the keys
      in `re=` count as answered.
  <!-- fabrikant-followup pr=<num> re=<key>,… -->
      In the body of a follow-up issue: origin (PR + comment keys), idempotency and limit per PR.
  <!-- fabrikant-escalate kind=<kind>[ re=<key>,…] -->
      Escalation to a human (ADR 0059); only the host writes it (agent text is
      stripped of all fabrikant markers). The escalation loop shows the latest one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

REVIEW_MARKER_RE = re.compile(r"<!-- fabrikant-review verdict=(approved|changes_requested) sha=([0-9a-f]{40}) -->")
REPLY_MARKER = "<!-- fabrikant-reply -->"  # old form without keys (still recognized)
REPLY_MARKER_RE = re.compile(r"<!-- fabrikant-reply(?: re=([\w,-]+))? -->")
VERDICT_APPROVED = "approved"
PAGE_SIZE = 100
MAX_INLINE = 10  # more inline comments = noise; the rest ends up in the summary
THREADS_LIMIT = 100  # GraphQL page; more open threads per PR is not a realistic case
HTTP_UNPROCESSABLE = "HTTP 422"
# Review states that carry a decision (COMMENTED/DISMISSED/PENDING do not).
DECISIVE_REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED")
REVIEW_EVENTS = {VERDICT_APPROVED: "APPROVE", "changes_requested": "REQUEST_CHANGES"}
COMMENT_EVENT = "COMMENT"  # own PRs (GitHub only allows comment reviews) or --comment-only
OVERFLOW_HEADING = "### Weitere Anmerkungen (nicht inline)"
HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# mergeStateStatus (GitHub GraphQL) → action. Every known status is mapped
# explicitly; unknown ones (new GitHub values) → wait with its own reason.
ACTION_MERGE, ACTION_REBASE, ACTION_WAIT = "merge", "resolve_conflicts", "wait"
MERGE_STATES = ("CLEAN", "HAS_HOOKS")  # mergeable, checks green (HAS_HOOKS: pre-receive hooks only)
REBASE_STATES = {
    "DIRTY": "Merge-Konflikt",
    "BEHIND": "Branch hinter Base (Rebase nötig)",
}
# ADR PRs with this label are merged only by the approval loop after human approval
# (ADR 0011) — pr-reply must not merge them despite a review approve.
APPROVAL_LABEL = "needs approval"
# Escalation to a human (ADR 0059): the label stops review-pr/pr-reply/develop and merge.
HUMAN_LABEL = "needs human"
ESCALATION_KINDS = ("blocked", "stalled", "exhausted", "scope", "access")
BUILDER_ESCALATION_KINDS = ("blocked", "scope")  # only these may be set by the builder via draft
ESCALATE_MARKER_RE = re.compile(r"<!-- fabrikant-escalate kind=(\w+)(?: re=([\w,-]+))? -->")
DEFAULT_STALL_ROUNDS = 2
WAIT_STATES = {
    "BLOCKED": "Branch-Protection blockiert (Pflicht-Review/-Checks) — Mensch nötig",
    "UNSTABLE": "Checks nicht grün",
    "DRAFT": "PR ist Draft",
    "UNKNOWN": "GitHub berechnet den Merge-Status noch",
}

THREADS_QUERY = """
query($owner: String!, $name: String!, $num: Int!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $num) {
      reviewThreads(first: $first) {
        nodes {
          id isResolved path line
          comments(first: 100) { nodes { databaseId body author { login } } }
        }
      }
    }
  }
}"""
RESOLVE_MUTATION = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"

# Catch network blips (DNS, resets) at the source (like fabrikant.netretry; the script runs without the
# fabrikant package). Reading calls retry on every network error, writing calls only if no connection
# was established — otherwise a comment could land twice. Sum < step timeout (120 s).
GH_RETRY_DELAYS_S: tuple[float, ...] = (3.0, 10.0, 30.0)
EX_TEMPFAIL = 75  # sysexits: the shell step classifies this as `transient` → run retry (ADR 0035)
_GH_NO_CONNECTION_RE = re.compile(
    r"error connecting to|no such host|server misbehaving|temporary failure in name resolution"
    r"|could not resolve|connection refused|network is unreachable",
    re.IGNORECASE,
)
_GH_TRANSIENT_RE = re.compile(
    _GH_NO_CONNECTION_RE.pattern + r"|i/o timeout|connection reset|tls handshake timeout|unexpected eof"
    r"|HTTP 50[234]|HTTP 429",
    re.IGNORECASE,
)
_READ_METHODS = frozenset({"GET", "HEAD"})


class GhError(subprocess.CalledProcessError):
    """Failed `gh` call; unlike CalledProcessError, with gh's error message in the text."""

    def __str__(self) -> str:
        detail = " ".join(str(self.stderr or "").split())[:500]
        return f"gh {' '.join(self.cmd[1:3])} fehlgeschlagen (exit {self.returncode}): {detail}"

    @property
    def transient(self) -> bool:
        return bool(_GH_TRANSIENT_RE.search(str(self.stderr or "")))


def _gh_is_write(args: tuple[str, ...]) -> bool:
    """Does the call change something on GitHub? (`gh api` with fields/body and no method is a POST.)"""
    if not args or args[0] != "api":
        return args[:2] not in {("pr", "view"), ("pr", "diff"), ("issue", "view")}
    if "graphql" in args:
        return any("mutation" in a for a in args)
    for flag in ("--method", "-X"):
        if flag in args and args.index(flag) + 1 < len(args):
            return args[args.index(flag) + 1].upper() not in _READ_METHODS
    return any(a in ("-f", "-F", "--field", "--raw-field", "--input") for a in args)


def _gh_text(*args: str, stdin: str | None = None) -> str:
    """Run `gh <args>` and return stdout (error → GhError). Network blips are retried with
    GH_RETRY_DELAYS_S (writing calls only if no connection was established)."""
    retry_re = _GH_NO_CONNECTION_RE if _gh_is_write(args) else _GH_TRANSIENT_RE
    for attempt in range(len(GH_RETRY_DELAYS_S) + 1):
        p = subprocess.run(["gh", *args], capture_output=True, text=True, input=stdin)
        if p.returncode == 0:
            return p.stdout
        if attempt == len(GH_RETRY_DELAYS_S) or not retry_re.search(p.stderr or ""):
            raise GhError(p.returncode, ["gh", *args], p.stdout, p.stderr)
        warn(f"gh {' '.join(args[:2])}: Netzfehler, Versuch {attempt + 2} in {GH_RETRY_DELAYS_S[attempt]:.0f}s")
        time.sleep(GH_RETRY_DELAYS_S[attempt])
    raise AssertionError("unreachable")  # pragma: no cover


def _gh_json(*args: str, stdin: str | None = None):
    """Like _gh_text, stdout parsed as JSON."""
    return json.loads(_gh_text(*args, stdin=stdin))


def fetch_paged(path: str) -> list[dict]:
    """All entries of a paginated REST list (`per_page=&page=`; `path` may already carry query parameters)."""
    items: list[dict] = []
    page = 1
    sep = "&" if "?" in path else "?"
    while True:
        batch = _gh_json("api", f"{path}{sep}per_page={PAGE_SIZE}&page={page}")
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            return items
        page += 1


REVIEWER_LOGIN_ENV = "FABRIKANT_REVIEWER_LOGIN"
BOT_PREFIX, BOT_SUFFIX = "app/", "[bot]"
# = fabrikant's runtime warning marker (the script runs without the fabrikant package): the shell step
# shows such lines as ⚠ in the run log. On stderr, so that the JSON on stdout stays parseable.
WARNING_MARKER = "::fabrikant-warning::"
_warned: set[str] = set()


def warn(msg: str) -> None:
    """Report a non-fatal problem — once per process (some checks run more than once)."""
    line = " ".join(msg.split())
    if line not in _warned:
        _warned.add(line)
        print(f"{WARNING_MARKER}{line}", file=sys.stderr)


def normalize_login(login: str) -> str:
    """Make bot logins comparable: `app/x` (gh JSON) and `x[bot]` (REST) → `x`."""
    return login.removeprefix(BOT_PREFIX).removesuffix(BOT_SUFFIX)


def reviewer_login() -> str:
    override = os.environ.get(REVIEWER_LOGIN_ENV, "").strip()
    return normalize_login(override or _gh_json("api", "user")["login"])


def _login(obj: dict) -> str:
    """Author login (normalized) from REST (`user`) or GraphQL/gh JSON (`author`)."""
    return normalize_login((obj.get("user") or obj.get("author") or {}).get("login", ""))


# ── ADR number collision (ADR 0032) ──

ADR_DIR = "fabrikant/docs/adr"  # repo-relative, as in `gh pr diff` and the contents API
ADR_FILE_RE = re.compile(r"^(\d{4})-.+\.md$")


def _adr_number(path: str) -> int | None:
    """Number of an ADR file under ADR_DIR (`…/0013-foo.md` → 13), otherwise None."""
    match = ADR_FILE_RE.match(Path(path).name) if Path(path).parent.as_posix() == ADR_DIR else None
    return int(match.group(1)) if match else None


def _added_adr_numbers(files: list[dict]) -> list[int]:
    """Numbers of newly created ADRs from `GET pulls/{n}/files`. Changes and renames
    with the same number do not count; a rename to a different number counts as new."""
    numbers: list[int] = []
    for f in files:
        number = _adr_number(f.get("filename", ""))
        if number is None:
            continue
        status = f.get("status")
        if status == "added" or (status == "renamed" and _adr_number(f.get("previous_filename", "")) != number):
            numbers.append(number)
    return numbers


def adr_collision(repo: str, num: int, pr: dict) -> str | None:
    """Collision description if PR `num` adds an ADR with a number taken on the base (or
    duplicated within the PR) — also catches PR-vs-PR collisions: once the first PR is
    merged, its number is on the base. Without `baseRefName` → None.

    File list via the paginated REST API (no `gh pr diff`: GitHub rejects diffs of very
    large PRs). If the check fails → description of the error (fail closed:
    merge-check waits instead of aborting with a traceback)."""
    base = pr.get("baseRefName")
    if not base:
        return None
    try:
        base_files = _gh_json("api", f"repos/{repo}/contents/{ADR_DIR}?ref={base}")
        files = fetch_paged(f"repos/{repo}/pulls/{num}/files")
    except (subprocess.CalledProcessError, ValueError) as e:
        return f"ADR-Nummern-Prüfung fehlgeschlagen ({e}) — merge-check später wiederholen"
    taken = {int(m.group(1)) for item in base_files if (m := ADR_FILE_RE.match(item.get("name", "")))}
    seen: set[int] = set()
    for number in _added_adr_numbers(files):
        if number in taken or number in seen:
            return f"ADR-Nummer {number:04d} kollidiert mit {base} — scripts/check-adr-numbers.py fix"
        seen.add(number)
    return None


# ── Verdict / merge ──


def review_verdict(reviews: list[dict], reviewer: str) -> tuple[str, str] | None:
    """Latest (verdict, sha) from review markers of the reviewer account, otherwise None.

    Markers from other authors are ignored — otherwise any commenter could
    fake an approval."""
    verdict = None
    for r in reviews:
        m = REVIEW_MARKER_RE.search(r.get("body") or "")
        if m and _login(r) == reviewer:
            verdict = (m.group(1), m.group(2))
    return verdict


def latest_plain_review_state(reviews: list[dict]) -> str:
    """Decisive state of the reviews WITHOUT fabrikant marker ('' if none).

    These are humans or the pr_review step (ADR 0010) — they count additionally.
    Per author only their latest decisive review counts (like GitHub's
    reviewDecision): an earlier CHANGES_REQUESTED is superseded as soon as the same
    author reviews decisively again afterwards — with or without a marker. Otherwise
    an old pr_review verdict would block the PR forever, because all later
    reviews of the same account carry markers. CHANGES_REQUESTED from one author
    beats APPROVED from another."""
    latest: dict[str, dict] = {}
    for r in reviews:
        if r.get("state") in DECISIVE_REVIEW_STATES:
            latest[_login(r)] = r
    plain = {str(r["state"]) for r in latest.values() if not REVIEW_MARKER_RE.search(r.get("body") or "")}
    for state in ("CHANGES_REQUESTED", "APPROVED"):
        if state in plain:
            return state
    return ""


def has_label(item: dict, label: str) -> bool:
    """Does the PR/issue (gh JSON or REST, `labels: [{name}]`) carry the label?"""
    return label in {str(lbl.get("name", "")) for lbl in item.get("labels") or []}


def merge_decision(pr: dict, reviewer: str, open_threads: int, human_label: str = HUMAN_LABEL) -> dict:
    """Merge decision from `gh pr view --json mergeStateStatus,headRefOid,reviews,labels`.

    Approval = review marker 'approved' for the current head SHA, otherwise a
    GitHub review APPROVED. Blocked by CHANGES_REQUESTED without marker
    (human/pr_review step) and by open review threads. Conflict/behind
    (REBASE_STATES) → resolve_conflicts regardless of approval. Label
    APPROVAL_LABEL → always wait (merge only via the approval loop, ADR 0011); `human_label`
    → wait (escalated, ADR 0059)."""
    status = pr.get("mergeStateStatus", "")
    if status in REBASE_STATES:
        return {"action": ACTION_REBASE, "reason": f"{REBASE_STATES[status]} (mergeStateStatus={status})"}
    if has_label(pr, APPROVAL_LABEL):
        return {
            "action": ACTION_WAIT,
            "reason": f"Label '{APPROVAL_LABEL}' — Merge nur über den approval-Loop (ADR 0011)",
        }
    if human_label and has_label(pr, human_label):
        return {"action": ACTION_WAIT, "reason": f"Label '{human_label}' — an einen Menschen eskaliert (ADR 0059)"}

    reviews = pr.get("reviews") or []
    plain = latest_plain_review_state(reviews)
    marker = review_verdict(reviews, reviewer)
    head = pr.get("headRefOid", "")
    if plain == "CHANGES_REQUESTED":
        approved, why = False, "GitHub-Review CHANGES_REQUESTED"
    elif marker is not None and marker[1] != head:
        approved, why = False, f"Review-Marker für alten SHA {marker[1][:7]} (Head {head[:7]})"
    elif marker is not None:
        approved, why = marker[0] == VERDICT_APPROVED, f"Review-Marker {marker[0]} @ {head[:7]}"
    elif plain == "APPROVED":
        approved, why = True, "GitHub-Review APPROVED"
    else:
        approved, why = False, "kein Review"

    if approved and open_threads:
        approved, why = False, f"{why}, aber {open_threads} offene Review-Threads"
    if not approved:
        return {"action": ACTION_WAIT, "reason": why}
    if status in MERGE_STATES:
        return {"action": ACTION_MERGE, "reason": why}
    blocker = WAIT_STATES.get(status, "unbekannter Merge-Status")
    return {"action": ACTION_WAIT, "reason": f"{why}, aber {blocker} (mergeStateStatus={status})"}


# ── Base sync ──

SYNC_UPDATED, SYNC_CURRENT, SYNC_CONFLICT, SYNC_PENDING = "updated", "up_to_date", "conflict", "pending"
CONFLICT_LABEL = "fabrikant-conflict"
MERGEABLE_STATE_DIRTY = "dirty"  # REST `mergeable_state`: merge conflict with the base


def sync_decision(pr: dict, behind_by: int) -> dict:
    """Action from REST `pulls/{n}` + `behind_by` (compare base...head); "update" = bring up to date.

    `mergeable: null` means GitHub is still computing → pending (the next run decides)."""
    if pr.get("mergeable") is False or pr.get("mergeable_state") == MERGEABLE_STATE_DIRTY:
        return {"action": SYNC_CONFLICT, "reason": "Merge-Konflikt mit der Base"}
    if pr.get("mergeable") is None:
        return {"action": SYNC_PENDING, "reason": "GitHub berechnet die Mergebarkeit noch"}
    if behind_by:
        return {"action": "update", "reason": f"{behind_by} Commit(s) hinter der Base"}
    return {"action": SYNC_CURRENT, "reason": "auf Base-Stand"}


def _set_label(repo: str, num: int, label: str, *, present: bool, labels: set[str]) -> None:
    """Add/remove a label, only when needed (GitHub creates a missing label when adding it)."""
    if present and label not in labels:
        _gh_text("api", "-X", "POST", f"repos/{repo}/issues/{num}/labels", "-f", f"labels[]={label}")
    elif not present and label in labels:
        _gh_text("api", "-X", "DELETE", f"repos/{repo}/issues/{num}/labels/{label}")


def sync_base(repo: str, num: int, conflict_label: str = CONFLICT_LABEL) -> dict:
    """Keep the PR up to date with its base: update-branch when behind, conflict label on a conflict."""
    pr = _gh_json("api", f"repos/{repo}/pulls/{num}")
    head = pr["head"]["sha"]
    labels = {str(lbl.get("name", "")) for lbl in pr.get("labels") or []}
    cmp = _gh_json("api", f"repos/{repo}/compare/{pr['base']['ref']}...{head}")
    result = sync_decision(pr, int(cmp.get("behind_by") or 0))
    if result["action"] == "update":
        try:
            _gh_text("api", "-X", "PUT", f"repos/{repo}/pulls/{num}/update-branch", "-f", f"expected_head_sha={head}")
            result["action"] = SYNC_UPDATED
        except subprocess.CalledProcessError as e:
            if HTTP_UNPROCESSABLE not in (e.stderr or ""):
                raise
            # 422: merge conflict — or the head has moved since it was read (expected_head_sha);
            # then the next run decides, instead of waking the conflict agent for nothing.
            conflict = "conflict" in (e.stderr or "").lower()
            result = {
                "action": SYNC_CONFLICT if conflict else SYNC_PENDING,
                "reason": f"update-branch abgelehnt (HTTP 422{', Konflikt' if conflict else ', Head bewegt'})",
            }
    if result["action"] != SYNC_PENDING:
        _set_label(repo, num, conflict_label, present=result["action"] == SYNC_CONFLICT, labels=labels)
    return {**result, "head": head}


# ── Posting a review ──


def _overflow_section(comments: list[dict]) -> str:
    notes = [f"**`{c.get('path')}:{c.get('line')}`**\n{c.get('body', '')}" for c in comments]
    return OVERFLOW_HEADING + "\n\n" + "\n\n".join(notes)


VERDICTS = tuple(REVIEW_EVENTS)  # approved | changes_requested
_SHA_RE = re.compile(r"[0-9a-f]{40}")


def with_verdict_marker(payload: dict, sha: str) -> dict:
    """Payload with verdict marker: if the body has none, the host builds it from the field `verdict` and
    the reviewed head SHA (`sha`, from the host). That way a forgotten marker does not sink a finished
    review; other fabrikant markers in the body (forged by the agent) are stripped. A marker in the body
    takes precedence (old format). ValueError if both are missing or invalid."""
    body = str(payload.get("body") or "")
    if REVIEW_MARKER_RE.search(body):
        return payload
    verdict = str(payload.get("verdict") or "").strip().lower().replace(" ", "_")
    if verdict not in VERDICTS:
        raise ValueError(f"Review ohne Verdikt: `verdict` muss {' | '.join(VERDICTS)} sein (ist {verdict!r})")
    if not _SHA_RE.fullmatch(sha or ""):
        raise ValueError("Review ohne Marker und ohne --sha: reviewter Commit unbekannt")
    marker = f"<!-- fabrikant-review verdict={verdict} sha={sha} -->"
    return {**payload, "body": f"{_ANY_FABRIKANT_MARKER_RE.sub('', body).rstrip()}\n{marker}"}


def _insert_before_marker(body: str, section: str) -> str:
    """Insert a section before the verdict marker (the marker stays the last line)."""
    m = REVIEW_MARKER_RE.search(body)
    if not m:
        return f"{body}\n\n{section}"
    return f"{body[: m.start()].rstrip()}\n\n{section}\n\n{body[m.start() :]}"


def diff_lines(diff: str) -> dict[str, list[set[int]]]:
    """Commentable lines (new state, RIGHT) from a unified diff: per file
    a set of lines per hunk (`+` and context lines)."""
    hunks: dict[str, list[set[int]]] = {}
    path, lineno, current = "", 0, None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[len("+++ b/") :] if line.startswith("+++ b/") else ""
            current = None
        elif m := HUNK_HEADER_RE.match(line):
            lineno, current = int(m.group(1)), set()
            hunks.setdefault(path, []).append(current)
        elif current is not None and line[:1] in (" ", "+"):
            current.add(lineno)
            lineno += 1
    return hunks


def in_diff(comment: dict, hunks: dict[str, list[set[int]]]) -> bool:
    """Do `line` (and, if given, `start_line`) lie in the same hunk of the file?"""
    wanted = {comment.get("line"), comment.get("start_line", comment.get("line"))}
    return any(wanted <= hunk for hunk in hunks.get(comment.get("path") or "", []))


def build_review(
    payload: dict, *, comment_only: bool, inline: bool = True, hunks: dict[str, list[set[int]]] | None = None
) -> dict:
    """GitHub review request from the agent payload `{"body", "comments"}`.

    - The marker in the body is mandatory (verdict + reviewed SHA = `commit_id`).
    - Event: `comment_only` (own PR or --comment-only) → COMMENT, otherwise APPROVE/REQUEST_CHANGES.
    - Inline comments: only lines in `hunks` (if given, see diff_lines),
      at most MAX_INLINE; the rest (or all with inline=False) goes into the summary as a list."""
    body = payload.get("body") or ""
    m = REVIEW_MARKER_RE.search(body)
    if not m:
        raise ValueError("Review-Body ohne fabrikant-review-Marker")
    verdict, sha = m.group(1), m.group(2)
    event = COMMENT_EVENT if comment_only else REVIEW_EVENTS[verdict]

    comments = [{"side": "RIGHT", **c} for c in payload.get("comments") or []]
    keep: list[dict] = []
    overflow: list[dict] = []
    for c in comments:
        anchored = inline and len(keep) < MAX_INLINE and (hunks is None or in_diff(c, hunks))
        (keep if anchored else overflow).append(c)
    if overflow:
        body = _insert_before_marker(body, _overflow_section(overflow))
    return {"commit_id": sha, "event": event, "body": body, "comments": keep}


REFS_HEADING = "### Referenced issues"


def with_issue_refs(payload: dict, refs: RefContext, issue_refs: tuple[IssueRef, ...]) -> dict:
    """Review payload with checked issue references (`issue_refs`, from `IssueRef.parse_all`; invalid ones
    are dropped) and defused free `#N` (summary and inline comments). The references form their own
    section before the verdict marker. The reviewer decides on their own — their
    `covered_by`/`duplicate_of` are not mere proposals."""
    issue_refs = refs.valid(issue_refs, "Review")
    allowed = refs.allowed_numbers(issue_refs)
    body = defuse_issue_refs(payload.get("body") or "", allowed)
    if issue_refs:
        body = _insert_before_marker(body, f"{REFS_HEADING}\n{refs.render(issue_refs, proposed=False)}")
    comments = [
        {**c, "body": defuse_issue_refs(str(c.get("body") or ""), allowed)} for c in payload.get("comments") or []
    ]
    return {**payload, "body": body, "comments": comments}


def already_reviewed(reviews: list[dict], reviewer: str, sha: str) -> bool:
    """Is there already a review by the reviewer with a marker for this SHA? (retry protection)"""
    return any(
        _login(r) == reviewer and (m := REVIEW_MARKER_RE.search(r.get("body") or "")) and m.group(2) == sha
        for r in reviews
    )


def review_check(pr: dict, reviewer: str, sha: str = "") -> dict:
    """Result of `review-check` from `gh pr view --json headRefOid,reviews` (see module doc)."""
    head = str(pr.get("headRefOid") or "")
    target = sha or head
    if already_reviewed(pr.get("reviews") or [], reviewer, target):
        return {"reviewed": True, "reason": f"Review von {reviewer} für {target[:7]} vorhanden", "head": head}
    if sha and sha != head:
        return {
            "reviewed": True,
            "reason": f"Head {sha[:7]} → {head[:7]} weitergezogen — nächster Run reviewt",
            "head": head,
        }
    return {"reviewed": False, "reason": f"kein Review von {reviewer} für {target[:7]} gepostet", "head": head}


def post_review(repo: str, num: int, payload: dict, *, comment_only: bool = False, sha: str = "") -> dict:
    """Post a review (see build_review); only for the current head SHA,
    idempotent per SHA, inline outside the diff → summary, 422 → without inline.
    `comment_only` forces a COMMENT review (own PRs always get one).
    `sha` = reviewed commit (host); with it, the field `verdict` in the payload suffices (with_verdict_marker)."""
    payload = with_verdict_marker(payload, sha)
    reviewer = reviewer_login()
    pr = _gh_json("pr", "view", str(num), "--repo", repo, "--json", "author,reviews,headRefOid")
    comment_only = comment_only or _login(pr) == reviewer
    issue_refs = IssueRef.parse_all(payload.get("refs"), "Review")
    ctx = RefContext(
        pr=num,
        issues=resolve_issues(repo, {r.issue for r in issue_refs}),
        closing=frozenset(closing_issues(repo, num)),
    )
    payload = with_issue_refs(payload, ctx, issue_refs)
    hunks = diff_lines(_gh_text("pr", "diff", str(num), "--repo", repo))
    request = build_review(payload, comment_only=comment_only, hunks=hunks)
    sha, head = request["commit_id"], pr.get("headRefOid", "")
    if sha != head:
        # Payload for an old commit: neither post nor report "done" via idempotency.
        return {"posted": False, "stale": True, "reason": f"Marker-SHA {sha[:7]} ≠ Head {head[:7]} — neu reviewen"}
    if already_reviewed(pr.get("reviews") or [], reviewer, head):
        return {"posted": False, "reason": f"Review für {head[:7]} existiert bereits"}

    endpoint = f"repos/{repo}/pulls/{num}/reviews"
    try:
        res = _gh_json("api", "--method", "POST", endpoint, "--input", "-", stdin=json.dumps(request))
        return {"posted": True, "url": res.get("html_url", ""), "inline": len(request["comments"])}
    except subprocess.CalledProcessError as e:
        if HTTP_UNPROCESSABLE not in (e.stderr or "") or not request["comments"]:
            raise
    request = build_review(payload, comment_only=comment_only, inline=False)
    res = _gh_json("api", "--method", "POST", endpoint, "--input", "-", stdin=json.dumps(request))
    return {"posted": True, "url": res.get("html_url", ""), "inline": 0, "fallback": "Inline abgelehnt (422)"}


# ── Threads ──


def fetch_threads(repo: str, num: int) -> list[dict]:
    owner, name = repo.split("/", 1)
    data = _gh_json(
        "api",
        "graphql",
        "-f",
        f"query={THREADS_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
        "-F",
        f"num={num}",
        "-F",
        f"first={THREADS_LIMIT}",
    )
    return data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]


def open_threads(threads: list[dict]) -> list[dict]:
    """Open threads compactly: id, path, line, author + text of the first comment."""
    out = []
    for t in threads:
        if t.get("isResolved"):
            continue
        first = (t.get("comments", {}).get("nodes") or [{}])[0]
        out.append(
            {
                "id": t["id"],
                "path": t.get("path"),
                "line": t.get("line"),
                "comment_id": first.get("databaseId"),
                "author": _login(first),
                "body": first.get("body", ""),
            }
        )
    return out


def thread_for_comment(threads: list[dict], comment_id: int) -> dict | None:
    """Thread containing the inline comment (REST ID = GraphQL databaseId)."""
    for t in threads:
        if any(c.get("databaseId") == comment_id for c in t.get("comments", {}).get("nodes") or []):
            return t
    return None


# ── New comments ──


def load_replied(path: Path) -> dict:
    """State `{"<key>": true}`; missing/broken → {}."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def collect_comments(num: int, issue: list[dict], reviews: list[dict], inline: list[dict]) -> list[dict]:
    """All three comment sources normalized. State keys: `<num>-<id>` (PR comment,
    compatible with the old state), `<num>-review-<id>`, `<num>-inline-<id>`."""
    items = [
        {"key": f"{num}-{c['id']}", "kind": "comment", "created": c.get("created_at", ""), "src": c} for c in issue
    ]
    items += [
        {"key": f"{num}-review-{r['id']}", "kind": "review", "created": r.get("submitted_at", ""), "src": r}
        for r in reviews
        if (r.get("body") or "").strip()  # reviews with inline comments only have no text of their own
    ]
    for c in inline:
        item = {"key": f"{num}-inline-{c['id']}", "kind": "inline", "created": c.get("created_at", ""), "src": c}
        item |= {"path": c.get("path"), "line": c.get("line") or c.get("original_line")}
        item["in_reply_to"] = c.get("in_reply_to_id")
        items.append(item)
    for item in items:
        src = item.pop("src")
        item |= {"id": src["id"], "author": _login(src), "body": src.get("body") or "", "url": src.get("html_url", "")}
        item["bot"] = ((src.get("user") or src.get("author") or {}).get("type") or "") == "Bot"
    return sorted(items, key=lambda i: i["created"])


# Receipts of machine reviewers (e.g. Codex): a summary per round or a status comment, the findings
# are inline comments. Counted as new comments, the builder would answer a receipt every round
# ("nothing to act on").
BOT_RECEIPT_MARKERS = ("<!-- codex-review-receipt", "<!-- codex-pull-request-review-summary")
_RE_URL_RE = re.compile(r"^\s*Re:\s*<?(\S+?)>?\s*$", re.MULTILINE)


def marker_replied_keys(items: list[dict]) -> set[str]:
    """Keys that our own replies (marker) mark as answered: explicitly via `re=`, otherwise
    implicitly — inline reply → thread root (`in_reply_to`), PR comment → `Re: <url>` line."""
    by_url = {i["url"]: i["key"] for i in items if i.get("url")}
    keys: set[str] = set()
    for i in items:
        marks = list(REPLY_MARKER_RE.finditer(i["body"]))
        if not marks:
            continue
        for m in marks:
            keys.update(k for k in (m.group(1) or "").split(",") if k)
        if i.get("in_reply_to"):
            keys.add(i["key"].rsplit("-", 1)[0] + f"-{i['in_reply_to']}")
        keys.update(by_url[u] for u in _RE_URL_RE.findall(i["body"]) if u in by_url)
    return keys


def is_bot_receipt(item: dict) -> bool:
    """Summary/receipt of a machine reviewer without findings of its own (PR comment or review text)."""
    return item["bot"] and item["kind"] != "inline" and any(m in item["body"] for m in BOT_RECEIPT_MARKERS)


def new_comments(
    items: list[dict], replied: dict | None = None, keep_bots: Collection[str] | None = None
) -> list[dict]:
    """Unanswered foreign comments: no own reply marker in the text, key neither in a
    `re=` marker on the PR nor in the (optional, old) state, no bot receipt (`is_bot_receipt`),
    no escalation comment of the host (marker; the human answers in the fabrikant gate).
    `keep_bots` set → bot comments (CI, code review bots) are dropped, except those of these accounts
    (normalized logins, e.g. QA reviewer app and Codex; empty = drop all bots)."""
    done = marker_replied_keys(items) | set(replied or {})
    return [
        i
        for i in items
        if i["key"] not in done
        and not REPLY_MARKER_RE.search(i["body"])
        and not ESCALATE_MARKER_RE.search(i["body"])  # host notice, not review input
        and not is_bot_receipt(i)
        and (keep_bots is None or not i["bot"] or i["author"] in keep_bots)
    ]


# ── Issue references, candidate list, follow-up issues ──
#
# Agents have no GitHub access. If they want to argue with existing issues, they name them
# structurally (`refs`); the host resolves and checks every number live when posting. Free `#N` in the
# text without backing are defused (code span instead of autolink) — otherwise GitHub links
# every number, including made-up ones. Follow-up issues are also created only by the host.

REF_RELATIONS = {"covered_by": "covered by", "duplicate_of": "duplicate of", "blocked_by": "blocked by"}
DEFER_RELATIONS = frozenset({"covered_by", "duplicate_of"})  # defer a point — only with reviewer consent
MAX_REFS = 3
MAX_REF_WHY = 200
MAX_TITLE_IN_REF = 100
# Deliberately NOT the `fabrikant` trigger label: refine/develop do not pick up follow-ups on their own.
FOLLOWUP_LABEL = "fabrikant-followup"
MAX_FOLLOWUPS_PER_PR = 2
MAX_FOLLOWUP_TITLE = 120
MAX_FOLLOWUP_BODY = 4000
FOLLOWUP_MARKER_RE = re.compile(r"<!-- fabrikant-followup pr=(\d+) re=([\w,-]+) -->")
COMPLETED = "completed"
_FREE_ISSUE_REF_RE = re.compile(r"(?<![\w/&`#])#(\d+)\b")
_FENCE_RE = re.compile(r"(```.*?```)", re.S)
DEFAULT_CLOSED_DAYS = 90
DEFAULT_EXCERPT_CHARS = 300
CLOSING_ISSUES_QUERY = """
query($owner: String!, $name: String!, $num: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $num) { closingIssuesReferences(first: 20) { nodes { number } } }
  }
}"""


@dataclass(frozen=True)
class IssueRef:
    """Structured reference of an agent to an existing issue."""

    issue: int
    relation: str
    why: str = ""

    @classmethod
    def parse(cls, raw: object, where: str) -> IssueRef:
        """Check one reference from agent output (untrusted). ValueError on an invalid shape."""
        issue = raw.get("issue") if isinstance(raw, dict) else None
        relation = raw.get("relation") if isinstance(raw, dict) else None
        why = (raw.get("why") if isinstance(raw, dict) else "") or ""
        if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
            raise ValueError(f"{where}: ref ohne gültige Issue-Nummer: {raw!r:.80}")
        if relation not in REF_RELATIONS:
            raise ValueError(f"{where}: relation {relation!r} nicht erlaubt ({sorted(REF_RELATIONS)})")
        if not isinstance(why, str) or len(why) > MAX_REF_WHY:
            raise ValueError(f"{where}: why muss Text mit höchstens {MAX_REF_WHY} Zeichen sein")
        return cls(issue, relation, _ANY_FABRIKANT_MARKER_RE.sub("", why).strip())

    @classmethod
    def parse_all(cls, raw: object, where: str) -> tuple[IssueRef, ...]:
        """`refs` from agent output (untrusted), lenient: `refs` are optional, one invalid
        entry should not sink the whole review/reply. Invalid entries (and everything beyond
        MAX_REFS) are dropped and reported via `warn`."""
        if raw is None:
            return ()
        if not isinstance(raw, list):
            warn(f"{where}: refs ist keine Liste, verworfen: {raw!r:.80}")
            return ()
        refs = []
        for r in raw:
            try:
                refs.append(cls.parse(r, where))
            except ValueError as e:
                warn(f"{e} — Verweis verworfen")
        if len(refs) > MAX_REFS:
            warn(f"{where}: {len(refs)} refs, nur die ersten {MAX_REFS} werden verwendet")
        return tuple(refs[:MAX_REFS])


@dataclass(frozen=True)
class FollowUp:
    """Follow-up issue proposed by the builder; the host creates it."""

    title: str
    body: str

    @classmethod
    def parse(cls, raw: object, where: str) -> FollowUp | None:
        if raw is None:
            return None
        title = raw.get("title") if isinstance(raw, dict) else None
        body = raw.get("body") if isinstance(raw, dict) else None
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_FOLLOWUP_TITLE:
            raise ValueError(f"{where}: follow_up.title fehlt oder ist länger als {MAX_FOLLOWUP_TITLE} Zeichen")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_FOLLOWUP_BODY:
            raise ValueError(f"{where}: follow_up.body fehlt oder ist länger als {MAX_FOLLOWUP_BODY} Zeichen")
        clean = _ANY_FABRIKANT_MARKER_RE.sub("", body)
        return cls(" ".join(title.split()), FOLLOWUP_MARKER_RE.sub("", clean).strip())


@dataclass(frozen=True)
class RefContext:
    """What the host knows about issues, to check and render references.

    issues: issues resolved live (number → REST object; missing = does not exist).
    closing: issues the PR closes (its own issues — neither blockers nor "already done by").
    followups: follow-up numbers, already existing or created in this run (keys → number)."""

    pr: int = 0
    issues: Mapping[int, dict] = field(default_factory=dict)
    closing: frozenset[int] = frozenset()
    followups: Mapping[str, int] = field(default_factory=dict)

    def allowed_numbers(self, refs: tuple[IssueRef, ...]) -> set[int]:
        return {r.issue for r in refs} | set(self.closing) | set(self.followups.values()) | {self.pr}

    def problem(self, r: IssueRef) -> str:
        """Why the reference is not valid against the resolved issues (``""`` = valid)."""
        item = self.issues.get(r.issue)
        if item is None:
            return f"#{r.issue} existiert nicht in diesem Repo"
        if "pull_request" in item:
            return f"#{r.issue} ist ein PR, kein Issue"
        if r.issue in self.closing:
            return f"#{r.issue} ist das eigene Issue dieses PRs"
        state, reason = item.get("state"), item.get("state_reason")
        if r.relation in DEFER_RELATIONS and not (state == "open" or reason == COMPLETED):
            return f"#{r.issue} ist {state}/{reason} — taugt nicht als {r.relation}"
        if r.relation == "blocked_by" and state != "open":
            return f"#{r.issue} ist nicht offen — kein Blocker"
        return ""

    def valid(self, refs: tuple[IssueRef, ...], where: str) -> tuple[IssueRef, ...]:
        """Only the valid references; report dropped ones via `warn` (see IssueRef.parse_all)."""
        keep = []
        for r in refs:
            if why := self.problem(r):
                warn(f"{where}: {why} — Verweis verworfen")
            else:
                keep.append(r)
        return tuple(keep)

    def render(self, refs: tuple[IssueRef, ...], *, proposed: bool) -> str:
        """Reference lines with title (defused) — the reader sees which issue is meant."""
        lines = []
        for r in refs:
            title = _safe_title(str(self.issues[r.issue].get("title") or ""))
            note = " — proposed, reviewer to confirm" if proposed and r.relation in DEFER_RELATIONS else ""
            why = f": {r.why}" if r.why else ""
            lines.append(f"→ {REF_RELATIONS[r.relation]} #{r.issue} “{title}”{why}{note}")
        return "\n".join(lines)


def _safe_title(title: str) -> str:
    """Issue title (untrusted) for a comment: single line, truncated, without markers/mentions/autolinks."""
    t = " ".join(_ANY_FABRIKANT_MARKER_RE.sub("", title).split())[:MAX_TITLE_IN_REF]
    return _FREE_ISSUE_REF_RE.sub(r"`#\1`", t.replace("@", "@​"))


def defuse_issue_refs(text: str, allowed: set[int]) -> str:
    """Write free `#N` outside code blocks that are not in `allowed` as a code span
    (no autolink to a possibly made-up issue)."""
    parts = _FENCE_RE.split(text)
    for i in range(0, len(parts), 2):  # odd indices = code blocks
        parts[i] = _FREE_ISSUE_REF_RE.sub(
            lambda m: m.group(0) if int(m.group(1)) in allowed else f"`{m.group(0)}`", parts[i]
        )
    return "".join(parts)


def closing_issues(repo: str, num: int) -> list[int]:
    """Issues that PR `num` closes according to GitHub (`Closes #N`)."""
    owner, name = repo.split("/", 1)
    data = _gh_json(
        "api",
        "graphql",
        "-f",
        f"query={CLOSING_ISSUES_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
        "-F",
        f"num={num}",
    )
    nodes = data["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
    return [int(n["number"]) for n in nodes]


def resolve_issues(repo: str, numbers: set[int]) -> dict[int, dict]:
    """Resolve issues live (missing/inaccessible ones are left out → count as non-existent)."""
    found = {}
    for n in sorted(numbers):
        try:
            found[n] = _gh_json("api", f"repos/{repo}/issues/{n}")
        except subprocess.CalledProcessError:
            continue
    return found


def existing_followups(repo: str, num: int) -> dict[str, int]:
    """Follow-ups the host has already created for PR `num`: keys (`re=`) → issue number."""
    out = {}
    for item in fetch_paged(f"repos/{repo}/issues?state=all&labels={FOLLOWUP_LABEL}"):
        m = FOLLOWUP_MARKER_RE.search(item.get("body") or "")
        if m and int(m.group(1)) == num and "pull_request" not in item:
            out[m.group(2)] = int(item["number"])
    return out


def followup_body(follow_up: FollowUp, *, num: int, keys: str, source_url: str) -> str:
    return (
        f"{follow_up.body}\n\n---\nFollow-up from #{num}"
        + (f" ({source_url})" if source_url else "")
        + ". Created by fabrikant; add the `fabrikant` label to let fabrikant refine and implement it.\n\n"
        + f"<!-- fabrikant-followup pr={num} re={keys} -->"
    )


def issue_candidates(items: list[dict], *, closed_since: str, excerpt: int) -> list[dict]:
    """Candidate list (lookup for agents, untrusted) from REST issues: open ones + those closed
    since `closed_since` (ISO), without PRs. Provider-neutral schema."""
    out = []
    for it in items:
        if "pull_request" in it:
            continue
        if it.get("state") == "closed" and str(it.get("closed_at") or "") < closed_since:
            continue
        body = " ".join(str(it.get("body") or "").split())
        out.append(
            {
                "id": str(it.get("number")),
                "number": it.get("number"),
                "url": it.get("html_url", ""),
                "title": it.get("title", ""),
                "state": it.get("state", ""),
                "state_reason": it.get("state_reason") or "",
                "labels": [str(lbl.get("name", "")) for lbl in it.get("labels") or []],
                "excerpt": body[:excerpt],
            }
        )
    return sorted(out, key=lambda c: c["number"] or 0)


def write_issue_candidates(repo: str, out: Path, *, closed_days: int, excerpt: int) -> dict:
    """`issue-candidates`: write the candidate list to `out`."""
    since = (datetime.now(UTC) - timedelta(days=closed_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = fetch_paged(f"repos/{repo}/issues?state=open") + fetch_paged(
        f"repos/{repo}/issues?state=closed&since={since}"
    )
    seen: dict[int, dict] = {it["number"]: it for it in items}
    candidates = issue_candidates(list(seen.values()), closed_since=since, excerpt=excerpt)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(candidates, ensure_ascii=False, indent=1))
    return {"written": str(out), "issues": len(candidates), "closed_since": since}


# ── PR reply: gate (before the agent) and posting (after the push) ──

REPLY_DIR_COMMENTS = "comments.json"
REPLY_DIR_CONTEXT = "context.json"
REPLY_DIR_DRAFTS = "replies.json"
FIX_COMMIT_PREFIX = "fabrikant: address review"  # commit message of the fix commits (round counter)
DEFAULT_MAX_REPLY_COMMENTS = 5
DEFAULT_MAX_FIX_ROUNDS = 3
COMMIT_PLACEHOLDER = "{commit}"
MAX_REPLY_CHARS = 6000
_ANY_FABRIKANT_MARKER_RE = re.compile(r"<!--\s*fabrikant-(?:reply|review|escalate|followup)\b[^>]*-->")
_COMMENT_FIELDS = ("key", "kind", "id", "author", "created", "body", "url", "path", "line", "in_reply_to")


def reply_context(
    pr: dict,
    comments: list[dict],
    fix_commits: list[dict],
    *,
    head_prefix: str,
    max_comments: int,
    max_rounds: int,
    human_label: str = HUMAN_LABEL,
) -> tuple[dict, list[dict]]:
    """(result, comments for the agent) from the PR state and the new comments."""
    head_ref = str(pr.get("headRefName") or "")
    if head_prefix and not head_ref.startswith(head_prefix):
        return {"action": "skip", "reason": f"Branch {head_ref!r} ist kein {head_prefix}*-PR"}, []
    if human_label and has_label(pr, human_label):
        return {"action": "skip", "reason": f"Label '{human_label}' — wartet auf einen Menschen (ADR 0059)"}, []
    if not comments:
        return {"action": "skip", "reason": "keine neuen Kommentare"}, []
    # Humans first (stable sort, otherwise chronological): an instruction from the team lands in the next
    # round, even if machine reviewers would already fill the limit.
    by_priority = sorted(comments, key=lambda c: bool(c.get("bot")))
    picked = [{k: c.get(k) for k in _COMMENT_FIELDS} for c in by_priority[:max_comments]]
    ctx = {
        "action": "reply",
        "reason": f"{len(picked)} von {len(comments)} neuen Kommentar(en)",
        "pr": pr.get("number"),
        "head_ref": head_ref,
        "head_sha": pr.get("headRefOid", ""),
        "base_ref": pr.get("baseRefName", ""),
        "fix_rounds": len(fix_commits),
        "fix_allowed": len(fix_commits) < max_rounds,
        "fix_commits": fix_commits,  # earlier fix commits: cite them in a re-run instead of {commit}
    }
    return ctx, picked


def _commit_message(c: dict) -> str:
    return str((c.get("commit") or {}).get("message") or "")


def _commit_date(c: dict) -> str:
    """Committer time (ISO, UTC) of a `GET pulls/{n}/commits` entry; GitHub signs API commits on push,
    so the time is the push."""
    return str(((c.get("commit") or {}).get("committer") or {}).get("date") or "")


def budget_start(issue_comments: list[dict]) -> str:
    """Start of the current fix budget (ADR 0059 §5, fresh budget after a resume): time of the last
    escalation comment (marker), otherwise ``""`` (= since the PR started). While the label is set,
    no fix commit is made — "since the last escalation" therefore means "since the last resume",
    and every resume gives the builder `max_rounds` rounds again."""
    stamps = [str(c.get("created_at") or "") for c in issue_comments if ESCALATE_MARKER_RE.search(c.get("body") or "")]
    return max(stamps, default="")


def _in_budget(c: dict, since: str) -> bool:
    return _commit_message(c).startswith(FIX_COMMIT_PREFIX) and (not since or _commit_date(c) > since)


def _fix_commits(repo: str, num: int, since: str = "") -> list[dict]:
    """Fix commits of the PR (`FIX_COMMIT_PREFIX`) in the current budget (after `since`, see budget_start):
    [{sha (short), message (first line)}]."""
    return [
        {"sha": str(c.get("sha", ""))[:7], "message": _commit_message(c).splitlines()[0]}
        for c in fetch_paged(f"repos/{repo}/pulls/{num}/commits")
        if _in_budget(c, since)
    ]


def write_reply_context(
    repo: str,
    num: int,
    out: Path,
    *,
    keep_bots: Collection[str],
    head_prefix: str,
    max_comments: int,
    max_rounds: int,
    human_label: str = HUMAN_LABEL,
) -> dict:
    """`reply-context`: write the gate files (see module doc)."""
    fields = "number,headRefName,headRefOid,baseRefName,labels"
    pr = _gh_json("pr", "view", str(num), "--repo", repo, "--json", fields)
    limits = {"head_prefix": head_prefix, "max_comments": max_comments, "max_rounds": max_rounds}
    gate = reply_context(pr, [], [], **limits, human_label=human_label)[0]
    if gate["reason"] != "keine neuen Kommentare":
        return gate  # branch prefix or escalation: do not even load the comments
    issue_comments = fetch_paged(f"repos/{repo}/issues/{num}/comments")
    items = collect_comments(
        num,
        issue_comments,
        fetch_paged(f"repos/{repo}/pulls/{num}/reviews"),
        fetch_paged(f"repos/{repo}/pulls/{num}/comments"),
    )
    fresh = new_comments(items, keep_bots=keep_bots)
    fixes = _fix_commits(repo, num, budget_start(issue_comments)) if fresh else []
    ctx, picked = reply_context(pr, fresh, fixes, **limits, human_label=human_label)
    if ctx["action"] == "reply":
        # For references and follow-ups (the agent cannot ask GitHub): own issues + existing follow-ups.
        ctx["closing_issues"] = closing_issues(repo, num)
        ctx["followups"] = [{"keys": k, "number": n} for k, n in existing_followups(repo, num).items()]
        ctx["followups_left"] = max(0, MAX_FOLLOWUPS_PER_PR - len(ctx["followups"]))
        out.mkdir(parents=True, exist_ok=True)
        (out / REPLY_DIR_COMMENTS).write_text(json.dumps(picked, ensure_ascii=False, indent=2))
        (out / REPLY_DIR_CONTEXT).write_text(json.dumps(ctx, ensure_ascii=False, indent=2))
        (out / REPLY_DIR_DRAFTS).unlink(missing_ok=True)  # never post drafts of an earlier run
    return ctx


@dataclass(frozen=True)
class ReplyDraft:
    """One reply draft of the agent: answers `keys` (from comments.json) with `body`."""

    keys: tuple[str, ...]
    body: str
    resolve: bool = False
    escalate: str = ""  # ADR 0059: "blocked" | "scope" — the host sets the label afterwards
    refs: tuple[IssueRef, ...] = ()  # references to existing issues (the host checks them live)
    follow_up: FollowUp | None = None  # follow-up issue (the host creates it)

    @classmethod
    def parse(cls, raw: object, known: dict[str, dict]) -> ReplyDraft:
        """Check a draft (agent output = untrusted). ValueError on an invalid draft."""
        if not isinstance(raw, dict):
            raise ValueError(f"Entwurf ist kein Objekt: {raw!r:.80}")
        keys = raw.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise ValueError(f"Entwurf ohne gültige keys: {raw!r:.80}")
        if unknown := [k for k in keys if k not in known]:
            raise ValueError(f"unbekannte keys {unknown} (nur aus comments.json)")
        body = raw.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError(f"Entwurf für {keys} ohne Text")
        if len(body) > MAX_REPLY_CHARS:
            raise ValueError(f"Entwurf für {keys} zu lang ({len(body)} > {MAX_REPLY_CHARS} Zeichen)")
        escalate = raw.get("escalate") or ""
        if escalate not in ("", *BUILDER_ESCALATION_KINDS):
            raise ValueError(f"Entwurf für {keys}: escalate {escalate!r} nicht erlaubt ({BUILDER_ESCALATION_KINDS})")
        body = _ANY_FABRIKANT_MARKER_RE.sub("", body).strip()
        where = f"Entwurf für {keys}"
        raw_refs = raw.get("refs")
        refs = IssueRef.parse_all(raw_refs, where)
        follow_up = FollowUp.parse(raw.get("follow_up"), where)
        # A deferred point is not done: deferral references/follow-ups resolve no thread
        # — the reviewer decides that in the next round. A dropped reference may have been
        # a deferral: then do not resolve either.
        dropped = raw_refs is not None and (not isinstance(raw_refs, list) or len(refs) < len(raw_refs))
        deferred = follow_up or dropped or any(r.relation in DEFER_RELATIONS for r in refs)
        resolve = bool(raw.get("resolve")) and not deferred
        return cls(tuple(keys), body, resolve, escalate, refs, follow_up)

    @property
    def key_str(self) -> str:
        return ",".join(self.keys)

    def thread(self, known: dict[str, dict]) -> int | None:
        """Root of the inline thread if all keys belong to exactly one — then reply in the thread,
        otherwise a PR comment with a `Re:` reference to all of them."""
        roots = {_thread_root(known[k]) if known[k]["kind"] == "inline" else None for k in self.keys}
        return next(iter(roots)) if len(roots) == 1 else None

    def render(self, known: dict[str, dict], commit: str, refs: RefContext | None = None) -> str:
        """Final text: `{commit}` replaced, unbacked `#N` defused, reference (`Re:`) outside a
        thread, checked references and follow-up link, marker at the end."""
        ctx = refs or RefContext()
        body = defuse_issue_refs(self.body.replace(COMMIT_PLACEHOLDER, commit), ctx.allowed_numbers(self.refs))
        if self.thread(known) is None:
            re_lines = "\n".join(f"Re: {known[k]['url']}" for k in self.keys if known[k].get("url"))
            body = f"{re_lines}\n\n{body}" if re_lines else body
        extra = [ctx.render(self.refs, proposed=True)] if self.refs else []
        if self.follow_up:
            number = ctx.followups.get(self.key_str)
            extra.append(f"→ follow-up #{number}" if number else "→ follow-up (pending)")
        if extra:
            body = f"{body}\n\n" + "\n".join(extra)
        keys = self.key_str
        escalation = f"\n{escalate_marker(self.escalate, keys)}" if self.escalate else ""
        return f"{body}\n\n<!-- fabrikant-reply re={keys} -->{escalation}"


def escalate_marker(kind: str, re_keys: str = "") -> str:
    """Escalation marker (ADR 0059); `re_keys` = comma-separated comment keys (optional)."""
    return f"<!-- fabrikant-escalate kind={kind}{f' re={re_keys}' if re_keys else ''} -->"


def _thread_root(comment: dict) -> int | None:
    return int(comment.get("in_reply_to") or comment["id"])


def plan_replies(
    drafts_raw: object,
    comments: list[dict],
    *,
    commit: str,
    answered: set[str],
    refs: RefContext | None = None,
    followups_left: int = MAX_FOLLOWUPS_PER_PR,
) -> tuple[list[tuple[ReplyDraft, str]], list[str], list[str]]:
    """(drafts to post with their text, skipped keys, unanswered keys). ValueError on
    invalid drafts, `{commit}` without a new commit, or more new follow-ups than `followups_left`.
    Invalid issue references (against `refs`) are dropped with a warning."""
    known = {c["key"]: c for c in comments}
    ctx = refs or RefContext()
    if not isinstance(drafts_raw, list):
        raise ValueError("replies.json muss eine Liste von Entwürfen sein")
    drafts = [ReplyDraft.parse(d, known) for d in drafts_raw]
    if not commit and any(COMMIT_PLACEHOLDER in d.body for d in drafts):
        raise ValueError(f"Entwurf nennt {COMMIT_PLACEHOLDER}, aber dieser Run hat nichts committet")
    covered = {k for d in drafts for k in d.keys}
    if dup := [k for k in covered if sum(k in d.keys for d in drafts) > 1]:
        raise ValueError(f"keys mehrfach beantwortet: {sorted(dup)}")
    drafts = [replace(d, refs=ctx.valid(d.refs, f"Entwurf für {list(d.keys)}")) for d in drafts]
    new_followups = [d for d in drafts if d.follow_up and d.key_str not in ctx.followups]
    if len(new_followups) > followups_left:
        raise ValueError(
            f"{len(new_followups)} neue Follow-ups, erlaubt noch {followups_left} (max. {MAX_FOLLOWUPS_PER_PR} pro PR)"
            " — bei mehr Restarbeit eskalieren (scope)"
        )
    todo = [(d, d.render(known, commit, ctx)) for d in drafts if not set(d.keys) <= answered]
    skipped = sorted(k for d in drafts if set(d.keys) <= answered for k in d.keys)
    unanswered = [c["key"] for c in comments if c["key"] not in covered]
    return todo, skipped, unanswered


def _post_comment(repo: str, num: int, text: str, root: int | None) -> dict:
    """Reply in the inline thread (`root`) or as a PR comment."""
    endpoint = (
        f"repos/{repo}/pulls/{num}/comments/{root}/replies"
        if root is not None
        else f"repos/{repo}/issues/{num}/comments"
    )
    return _gh_json("api", "--method", "POST", endpoint, "--input", "-", stdin=json.dumps({"body": text}))


def _create_followups(
    repo: str, num: int, drafts: list[ReplyDraft], known: dict[str, dict], have: dict[str, int]
) -> dict[str, int]:
    """Create missing follow-up issues (label FOLLOWUP_LABEL, marker for idempotency) → keys → number."""
    out = dict(have)
    for d in drafts:
        if not d.follow_up or d.key_str in out:
            continue
        source = next((known[k].get("url", "") for k in d.keys if known[k].get("url")), "")
        payload = {
            "title": d.follow_up.title,
            "body": followup_body(d.follow_up, num=num, keys=d.key_str, source_url=source),
            "labels": [FOLLOWUP_LABEL],
        }
        res = _gh_json("api", "--method", "POST", f"repos/{repo}/issues", "--input", "-", stdin=json.dumps(payload))
        out[d.key_str] = int(res["number"])
    return out


def post_replies(repo: str, num: int, directory: Path, *, commit: str, human_label: str = HUMAN_LABEL) -> dict:
    """`post-replies`: check and post the drafts (see module doc).

    Two phases, so that an invalid draft leaves no orphaned follow-up issues behind:
    first check everything (including issue references resolved live and the follow-up limit), then
    create the follow-ups, then post. If a draft escalates, the host then sets the label `human_label`."""
    comments = json.loads((directory / REPLY_DIR_COMMENTS).read_text())
    context_path = directory / REPLY_DIR_CONTEXT
    context = json.loads(context_path.read_text()) if context_path.exists() else {}
    drafts_path = directory / REPLY_DIR_DRAFTS
    drafts_raw = json.loads(drafts_path.read_text()) if drafts_path.exists() else []
    known = {c["key"]: c for c in comments}
    parsed = [ReplyDraft.parse(d, known) for d in drafts_raw] if isinstance(drafts_raw, list) else []
    have = existing_followups(repo, num) if any(d.follow_up for d in parsed) else {}
    ctx = RefContext(
        pr=num,
        issues=resolve_issues(repo, {r.issue for d in parsed for r in d.refs}),
        closing=frozenset(int(n) for n in context.get("closing_issues") or []),
        followups=have,
    )
    items = collect_comments(
        num,
        fetch_paged(f"repos/{repo}/issues/{num}/comments"),
        fetch_paged(f"repos/{repo}/pulls/{num}/reviews"),
        fetch_paged(f"repos/{repo}/pulls/{num}/comments"),
    )
    answered = marker_replied_keys(items)
    left = max(0, MAX_FOLLOWUPS_PER_PR - len(set(have.values())))
    plan_replies(
        drafts_raw, comments, commit=commit, answered=answered, refs=ctx, followups_left=left
    )  # phase 1: check
    ctx = replace(ctx, followups=_create_followups(repo, num, parsed, known, have))
    todo, skipped, unanswered = plan_replies(
        drafts_raw, comments, commit=commit, answered=answered, refs=ctx, followups_left=left
    )
    posted = []
    for draft, text in todo:
        root = draft.thread(known)
        res = _post_comment(repo, num, text, root)
        if (
            root is not None
            and draft.resolve
            and commit
            and (thread := thread_for_comment(fetch_threads(repo, num), root))
        ):
            _gh_json("api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-f", f"id={thread['id']}")
        posted.append({"keys": list(draft.keys), "url": res.get("html_url", "")})
    result: dict = {"posted": posted, "skipped": skipped, "unanswered": unanswered}
    created = {k: n for k, n in ctx.followups.items() if k not in have}
    if created:
        result["followups"] = created
    escalations = [d.escalate for d in parsed if d.escalate]
    if escalations and human_label:
        _gh_text("api", "-X", "POST", f"repos/{repo}/issues/{num}/labels", "-f", f"labels[]={human_label}")
        result["escalated"] = escalations[0]
    return result


# ── Escalation (ADR 0059) ──

ESCALATE, CONTINUE = "escalate", "continue"


def _verdicts(reviews: list[dict], reviewer: str, since: str = "") -> list[tuple[str, str]]:
    """All (verdict, sha) from marker reviews of the reviewer account, chronological; with `since`
    (see budget_start) only reviews after it. Time from gh JSON (`submittedAt`) or REST."""
    out = []
    for r in reviews:
        m = REVIEW_MARKER_RE.search(r.get("body") or "")
        stamp = str(r.get("submittedAt") or r.get("submitted_at") or "")
        if m and _login(r) == reviewer and (not since or stamp > since):
            out.append((m.group(1), m.group(2)))
    return out


def _builder_changed(commits: list[dict], since: str, until: str) -> bool:
    """Is there a non-merge commit between `since` (exclusive) and `until` (inclusive)?
    `commits` = `GET pulls/{n}/commits` (chronological). Base syncs (`update-branch`) are
    merge commits and do not count as a change by the builder. Unknown SHAs (force push)
    → True, so that a rewritten branch never counts as a stall."""
    shas = [str(c.get("sha", "")) for c in commits]
    if since not in shas or until not in shas:
        return True
    window = commits[shas.index(since) + 1 : shas.index(until) + 1]
    return any(len(c.get("parents") or []) < 2 for c in window)


def escalation_decision(
    reviews: list[dict],
    commits: list[dict],
    *,
    reviewer: str,
    head: str,
    stall_rounds: int = DEFAULT_STALL_ROUNDS,
    max_rounds: int = 0,
    since: str = "",
) -> dict:
    """Deterministic escalation from review markers and PR commits (ADR 0059 §4). Only what happened
    since `since` counts (start of the current budget, see budget_start) — verdicts and fix commits
    before a resume do not trigger another escalation.

    stalled:   the last `stall_rounds` verdicts are `changes_requested`, and the builder has
               changed no code in between — nor after the last verdict up to the head
               (see _builder_changed).
    exhausted: `max_rounds` > 0 fix commits (`FIX_COMMIT_PREFIX`) used up and the verdict
               at the current head is still `changes_requested`."""
    verdicts = _verdicts(reviews, reviewer, since)
    if not verdicts or verdicts[-1][0] == VERDICT_APPROVED:
        return {"action": CONTINUE, "reason": "kein offenes changes_requested"}
    rounds = max(2, stall_rounds)
    tail = verdicts[-rounds:]
    stuck = (
        len(tail) == rounds
        and all(v == "changes_requested" for v, _ in tail)
        and not any(_builder_changed(commits, a, b) for (_, a), (_, b) in zip(tail, tail[1:], strict=False))
        # Also AFTER the last verdict: if the builder has pushed since (review pending),
        # it is active — no stall (consistent with `exhausted` below, which also
        # checks the current head).
        and not _builder_changed(commits, tail[-1][1], head)
    )
    if stuck:
        shas = " → ".join(sha[:7] for _, sha in tail)
        return {
            "action": ESCALATE,
            "kind": "stalled",
            "reason": f"{rounds} Runden changes_requested ohne Code-Änderung des Builders ({shas})",
        }
    fixes = sum(1 for c in commits if _in_budget(c, since))
    if max_rounds and fixes >= max_rounds and verdicts[-1] == ("changes_requested", head):
        return {
            "action": ESCALATE,
            "kind": "exhausted",
            "reason": (
                f"{fixes} Fix-Runden verbraucht (Limit {max_rounds}) seit der letzten Eskalation, "
                f"Verdikt am Head {head[:7]} weiter changes_requested"
            ),
        }
    return {"action": CONTINUE, "reason": f"Verdikt changes_requested, {fixes} Fix-Runde(n), kein Stillstand"}


def escalation_comment(kind: str, why: str) -> str:
    """Text of the escalation comment (visible + marker for the escalation loop)."""
    return (
        f"**fabrikant: an einen Menschen eskaliert** (`{kind}`)\n\n{why}\n\n"
        "Review, Reply und Merge pausieren, bis das Label entfernt ist. "
        "Entscheidung im fabrikant-Gate: Fortsetzen, Neu zuschneiden oder Aufgeben.\n\n"
        f"{escalate_marker(kind)}"
    )


def escalate(
    repo: str, num: int, kind: str, why: str, human_label: str = HUMAN_LABEL, *, builder: bool = False
) -> dict:
    """Post a comment with marker and set the label; idempotent via the label.

    `builder=True` (builder draft, ADR 0059 §3) restricts `kind` to
    BUILDER_ESCALATION_KINDS — only the host may set `stalled`/`exhausted`/`access`.
    This gives the agent no additional rights."""
    allowed = BUILDER_ESCALATION_KINDS if builder else ESCALATION_KINDS
    if kind not in allowed:
        raise ValueError(f"unbekannte Eskalationsart {kind!r} (erlaubt: {allowed})")
    item = _gh_json("api", f"repos/{repo}/issues/{num}")
    if has_label(item, human_label):
        return {"escalated": False, "reason": f"Label '{human_label}' schon gesetzt"}
    res = _gh_json(
        "api",
        "--method",
        "POST",
        f"repos/{repo}/issues/{num}/comments",
        "--input",
        "-",
        stdin=json.dumps({"body": escalation_comment(kind, why)}),
    )
    _gh_text("api", "-X", "POST", f"repos/{repo}/issues/{num}/labels", "-f", f"labels[]={human_label}")
    return {"escalated": True, "kind": kind, "url": res.get("html_url", "")}


def escalation_check(
    repo: str, num: int, *, stall_rounds: int, max_rounds: int, apply: bool, human_label: str = HUMAN_LABEL
) -> dict:
    """`escalation-check`: decision from the PR state, escalate right away with `apply`."""
    pr = _gh_json("pr", "view", str(num), "--repo", repo, "--json", "headRefOid,reviews,labels")
    if human_label and has_label(pr, human_label):
        return {"action": CONTINUE, "reason": f"Label '{human_label}' schon gesetzt"}
    result = escalation_decision(
        pr.get("reviews") or [],
        fetch_paged(f"repos/{repo}/pulls/{num}/commits"),
        reviewer=reviewer_login(),
        head=str(pr.get("headRefOid") or ""),
        stall_rounds=stall_rounds,
        max_rounds=max_rounds,
        since=budget_start(fetch_paged(f"repos/{repo}/issues/{num}/comments")),
    )
    if apply and result["action"] == ESCALATE:
        result |= escalate(repo, num, result["kind"], result["reason"], human_label)
    return result


# ── CLI ──


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    cmds = {
        name: sub.add_parser(name)
        for name in (
            "post-review",
            "review-check",
            "merge-check",
            "sync-base",
            "new-comments",
            "threads",
            "resolve-thread",
            "reply-context",
            "post-replies",
            "escalation-check",
            "escalate",
            "issue-candidates",
        )
    }
    for sp in cmds.values():
        sp.add_argument("num", type=int)
        sp.add_argument("--repo", required=True)
    cmds["post-review"].add_argument("--payload", type=Path, required=True)
    cmds["post-review"].add_argument("--comment-only", action="store_true")
    cmds["post-review"].add_argument("--sha", default="", help="reviewed commit; marker from `verdict` + SHA")
    cmds["review-check"].add_argument("--sha", default="", help="reviewed head (default: current head)")
    cmds["new-comments"].add_argument("--state", type=Path, help="old state file (optional, additional)")
    cmds["new-comments"].add_argument("--ignore-bots", action="store_true", help="leave out bot comments")
    cmds["new-comments"].add_argument(
        "--keep-bot",
        action="append",
        default=[],
        help="bot login that counts despite --ignore-bots (repeatable, e.g. QA reviewer app + Codex)",
    )
    cmds["resolve-thread"].add_argument("--comment-id", type=int, required=True)
    cmds["sync-base"].add_argument("--conflict-label", default=CONFLICT_LABEL)
    cmds["reply-context"].add_argument("--out", type=Path, required=True)
    cmds["reply-context"].add_argument("--head-prefix", default="")
    cmds["reply-context"].add_argument("--keep-bot", action="append", default=[])
    cmds["reply-context"].add_argument("--max-comments", type=int, default=DEFAULT_MAX_REPLY_COMMENTS)
    cmds["reply-context"].add_argument("--max-rounds", type=int, default=DEFAULT_MAX_FIX_ROUNDS)
    cmds["post-replies"].add_argument("--dir", type=Path, required=True)
    cmds["post-replies"].add_argument("--commit", default="", help="new commit of this run (empty = none)")
    for name in ("merge-check", "reply-context", "post-replies", "escalation-check", "escalate"):
        cmds[name].add_argument("--human-label", default=HUMAN_LABEL, help="Escalation label (ADR 0059)")
    cmds["escalation-check"].add_argument("--stall-rounds", type=int, default=DEFAULT_STALL_ROUNDS)
    cmds["escalation-check"].add_argument("--max-rounds", type=int, default=0, help="0 = no exhausted check")
    cmds["escalation-check"].add_argument("--escalate", action="store_true", help="on a hit, label + comment")
    cmds["escalate"].add_argument("--kind", required=True, choices=ESCALATION_KINDS)
    cmds["escalate"].add_argument("--why", required=True)
    cmds["escalate"].add_argument(
        "--builder",
        action="store_true",
        help="Builder draft (ADR 0059 §3): --kind only from BUILDER_ESCALATION_KINDS",
    )
    cmds["issue-candidates"].add_argument("--out", type=Path, required=True)
    cmds["issue-candidates"].add_argument("--closed-days", type=int, default=DEFAULT_CLOSED_DAYS)
    cmds["issue-candidates"].add_argument("--excerpt", type=int, default=DEFAULT_EXCERPT_CHARS)
    args = p.parse_args(argv)
    repo, num = args.repo, args.num

    result: object
    if args.cmd == "post-review":
        payload = json.loads(args.payload.read_text())
        result = post_review(repo, num, payload, comment_only=args.comment_only, sha=args.sha)
    elif args.cmd == "review-check":
        pr = _gh_json("pr", "view", str(num), "--repo", repo, "--json", "headRefOid,reviews")
        result = review_check(pr, reviewer_login(), args.sha)
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0 if result["reviewed"] else 1
    elif args.cmd == "merge-check":
        fields = "mergeStateStatus,headRefOid,reviews,baseRefName,labels"
        pr = _gh_json("pr", "view", str(num), "--repo", repo, "--json", fields)
        collision = adr_collision(repo, num, pr)
        result = (
            {"action": ACTION_WAIT, "reason": collision}
            if collision
            else merge_decision(pr, reviewer_login(), len(open_threads(fetch_threads(repo, num))), args.human_label)
        )
    elif args.cmd == "sync-base":
        result = sync_base(repo, num, args.conflict_label)
    elif args.cmd == "new-comments":
        items = collect_comments(
            num,
            fetch_paged(f"repos/{repo}/issues/{num}/comments"),
            fetch_paged(f"repos/{repo}/pulls/{num}/reviews"),
            fetch_paged(f"repos/{repo}/pulls/{num}/comments"),
        )
        # Explicit instead of `reviewer_login()`: as a builder app, `gh api user` must not be queried.
        keep_bots = {normalize_login(b) for b in args.keep_bot} if args.ignore_bots else None
        result = new_comments(items, load_replied(args.state) if args.state else None, keep_bots)
    elif args.cmd == "reply-context":
        result = write_reply_context(
            repo,
            num,
            args.out,
            keep_bots={normalize_login(b) for b in args.keep_bot},
            head_prefix=args.head_prefix,
            max_comments=args.max_comments,
            max_rounds=args.max_rounds,
            human_label=args.human_label,
        )
    elif args.cmd == "post-replies":
        try:
            result = post_replies(repo, num, args.dir, commit=args.commit, human_label=args.human_label)
        except ValueError as e:
            print(f"post-replies: {e}", file=sys.stderr)
            return 1
    elif args.cmd == "escalation-check":
        result = escalation_check(
            repo,
            num,
            stall_rounds=args.stall_rounds,
            max_rounds=args.max_rounds,
            apply=args.escalate,
            human_label=args.human_label,
        )
    elif args.cmd == "escalate":
        result = escalate(repo, num, args.kind, args.why, args.human_label, builder=args.builder)
    elif args.cmd == "issue-candidates":
        result = write_issue_candidates(repo, args.out, closed_days=args.closed_days, excerpt=args.excerpt)
    elif args.cmd == "threads":
        result = open_threads(fetch_threads(repo, num))
    else:
        thread = thread_for_comment(fetch_threads(repo, num), args.comment_id)
        if thread is None:
            print(f"kein Review-Thread mit Kommentar {args.comment_id}", file=sys.stderr)
            return 1
        _gh_json("api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-f", f"id={thread['id']}")
        result = {"resolved": True, "thread": thread["id"]}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def _entry() -> int:
    """CLI entry: a gh error that is still transient after all retries ends with EX_TEMPFAIL,
    so that fabrikant classifies it as `transient` (run retry) instead of `unknown`."""
    try:
        return main()
    except GhError as e:
        if not e.transient:
            raise
        print(f"pr-status: {e}", file=sys.stderr)
        return EX_TEMPFAIL


if __name__ == "__main__":
    sys.exit(_entry())
