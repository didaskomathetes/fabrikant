#!/usr/bin/env python3
"""pr-status — deterministic PR decisions for review-pr/pr-reply (ADR 0024).

The loop agents should not assemble reviews themselves nor derive merge approval
or "new comments" from raw JSON (LLM guessing = self-loops,
double posts, wrong merges). This script encapsulates that via `gh`:

  post-review <num> --repo R --payload state/review-<num>.json [--comment-only]
      Post one GitHub review (summary + inline comments) — only for the
      current head SHA, idempotent per SHA, at most MAX_INLINE inline comments.
      `--comment-only`: always COMMENT instead of APPROVE/REQUEST_CHANGES — for repos in
      which the verdict stays advisory and humans approve (team repos with CODEOWNERS).
      Inline lines outside the diff hunks are moved into the summary beforehand;
      422 from GitHub → fallback without any inline comments.
  merge-check <num> --repo R
      → {"action": "merge" | "resolve_conflicts" | "wait", "reason": "..."}
      (mergeStateStatus mapping is explicit: MERGE_STATES / REBASE_STATES / WAIT_STATES).
      If the PR adds an ADR with a number already taken on the base → "wait"
      (ADR 0032; repair: `scripts/check-adr-numbers.py fix`). Label
      'needs approval' → "wait": such PRs are merged only by the approval loop (ADR 0011).
      Label `needs human` (`--human-label`) → "wait": escalated (ADR 0059).
  new-comments <num> --repo R --state state/replied-comments.json
      → [{"key", "kind", "id", "author", "created", "body", ...}] (chronological)
  escalation-check <num> --repo R [--stall-rounds N] [--max-rounds N] [--escalate] [--human-label L]
      Deterministic escalation (ADR 0059, no LLM): `stalled` = the last N verdicts of the
      reviewer are `changes_requested` without a non-merge commit in between;
      `exhausted` = `--max-rounds` fix commits used up and still `changes_requested` at the head.
      → {"action": "escalate" | "continue", "kind": "...", "reason": "..."}; `--escalate` then
      sets label + marker comment (see `escalate`).
  escalate <num> --repo R --kind K --why TEXT [--human-label L] [--builder]
      Escalation to a human (ADR 0059): comment with marker + label L (default
      `needs human`). Idempotent: if the item already has the label, nothing happens.
      `--builder` (builder draft, ADR 0059 §3): `--kind` only from BUILDER_ESCALATION_KINDS
      (`blocked`/`scope`) — only the host may set `stalled`/`exhausted`/`access`.
  threads <num> --repo R
      → open (unresolved) review threads
  resolve-thread <num> --repo R --comment-id <id>
      Mark the review thread containing this inline comment as resolved.

Reviewer account: `FABRIKANT_REVIEWER_LOGIN` (required for GitHub App tokens — they may not
query `/user`), otherwise `gh api user`. Bot logins are normalized
(`app/<name>` from gh JSON ≡ `<name>[bot]` from REST ≡ `<name>`).

Markers (HTML comments, invisible in the rendered text):
  <!-- fabrikant-review verdict=approved|changes_requested sha=<40 hex> -->
      Verdict in the review body. Valid only from the reviewer account (`gh api user`) and
      only for the current head SHA — new commits need a new review.
      Needed because GitHub forbids approve/request-changes on one's own PRs.
  <!-- fabrikant-reply -->
      Reply of the pr-reply loop. Such comments are never "new" (no self-loop).
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
from pathlib import Path

REVIEW_MARKER_RE = re.compile(r"<!-- fabrikant-review verdict=(approved|changes_requested) sha=([0-9a-f]{40}) -->")
REPLY_MARKER = "<!-- fabrikant-reply -->"
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


def _gh_text(*args: str, stdin: str | None = None) -> str:
    """Run `gh <args>` and return stdout (error → CalledProcessError)."""
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, input=stdin).stdout


def _gh_json(*args: str, stdin: str | None = None):
    """Like _gh_text, stdout parsed as JSON."""
    return json.loads(_gh_text(*args, stdin=stdin))


def fetch_paged(path: str) -> list[dict]:
    """All entries of a paginated REST list (`?per_page=&page=`)."""
    items: list[dict] = []
    page = 1
    while True:
        batch = _gh_json("api", f"{path}?per_page={PAGE_SIZE}&page={page}")
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            return items
        page += 1


REVIEWER_LOGIN_ENV = "FABRIKANT_REVIEWER_LOGIN"
BOT_PREFIX, BOT_SUFFIX = "app/", "[bot]"


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


# ── Posting a review ──


def _overflow_section(comments: list[dict]) -> str:
    notes = [f"**`{c.get('path')}:{c.get('line')}`**\n{c.get('body', '')}" for c in comments]
    return OVERFLOW_HEADING + "\n\n" + "\n\n".join(notes)


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


def already_reviewed(reviews: list[dict], reviewer: str, sha: str) -> bool:
    """Is there already a review by the reviewer with a marker for this SHA? (retry protection)"""
    return any(
        _login(r) == reviewer and (m := REVIEW_MARKER_RE.search(r.get("body") or "")) and m.group(2) == sha
        for r in reviews
    )


def post_review(repo: str, num: int, payload: dict, *, comment_only: bool = False) -> dict:
    """Post a review (see build_review); only for the current head SHA,
    idempotent per SHA, inline outside the diff → summary, 422 → without inline.
    `comment_only` forces a COMMENT review (own PRs always get one)."""
    reviewer = reviewer_login()
    pr = _gh_json("pr", "view", str(num), "--repo", repo, "--json", "author,reviews,headRefOid")
    comment_only = comment_only or _login(pr) == reviewer
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
    return sorted(items, key=lambda i: i["created"])


def new_comments(items: list[dict], replied: dict) -> list[dict]:
    """Unanswered foreign comments: not in the state, not our own reply (marker)."""
    return [i for i in items if i["key"] not in replied and REPLY_MARKER not in i["body"]]


# ── Escalation (ADR 0059) ──

ESCALATE, CONTINUE = "escalate", "continue"
FIX_COMMIT_PREFIX = "fabrikant: address review"  # commit message of the fix commits (round counter)


def _verdicts(reviews: list[dict], reviewer: str) -> list[tuple[str, str]]:
    """All (verdict, sha) from marker reviews of the reviewer account, chronological."""
    out = []
    for r in reviews:
        m = REVIEW_MARKER_RE.search(r.get("body") or "")
        if m and _login(r) == reviewer:
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
) -> dict:
    """Deterministic escalation from review markers and PR commits (ADR 0059 §4).

    stalled:   the last `stall_rounds` verdicts are `changes_requested`, and the builder has
               changed no code in between — nor after the last verdict up to the head
               (see _builder_changed).
    exhausted: `max_rounds` > 0 fix commits (`FIX_COMMIT_PREFIX`) used up and the verdict
               at the current head is still `changes_requested`."""
    verdicts = _verdicts(reviews, reviewer)
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
    fixes = sum(1 for c in commits if str((c.get("commit") or {}).get("message", "")).startswith(FIX_COMMIT_PREFIX))
    if max_rounds and fixes >= max_rounds and verdicts[-1] == ("changes_requested", head):
        return {
            "action": ESCALATE,
            "kind": "exhausted",
            "reason": f"{fixes} Fix-Runden verbraucht (Limit {max_rounds}), Verdikt am Head {head[:7]} weiter changes_requested",
        }
    return {"action": CONTINUE, "reason": f"Verdikt changes_requested, {fixes} Fix-Runde(n), kein Stillstand"}


def escalate_marker(kind: str, re_keys: str = "") -> str:
    """Escalation marker (ADR 0059); `re_keys` = comma-separated comment keys (optional)."""
    return f"<!-- fabrikant-escalate kind={kind}{f' re={re_keys}' if re_keys else ''} -->"


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
            "merge-check",
            "new-comments",
            "threads",
            "resolve-thread",
            "escalation-check",
            "escalate",
        )
    }
    for sp in cmds.values():
        sp.add_argument("num", type=int)
        sp.add_argument("--repo", required=True)
    cmds["post-review"].add_argument("--payload", type=Path, required=True)
    cmds["post-review"].add_argument("--comment-only", action="store_true")
    cmds["new-comments"].add_argument("--state", type=Path, required=True)
    cmds["resolve-thread"].add_argument("--comment-id", type=int, required=True)
    for name in ("merge-check", "escalation-check", "escalate"):
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
    args = p.parse_args(argv)
    repo, num = args.repo, args.num

    result: object
    if args.cmd == "post-review":
        result = post_review(repo, num, json.loads(args.payload.read_text()), comment_only=args.comment_only)
    elif args.cmd == "merge-check":
        fields = "mergeStateStatus,headRefOid,reviews,baseRefName,labels"
        pr = _gh_json("pr", "view", str(num), "--repo", repo, "--json", fields)
        collision = adr_collision(repo, num, pr)
        result = (
            {"action": ACTION_WAIT, "reason": collision}
            if collision
            else merge_decision(pr, reviewer_login(), len(open_threads(fetch_threads(repo, num))), args.human_label)
        )
    elif args.cmd == "new-comments":
        items = collect_comments(
            num,
            fetch_paged(f"repos/{repo}/issues/{num}/comments"),
            fetch_paged(f"repos/{repo}/pulls/{num}/reviews"),
            fetch_paged(f"repos/{repo}/pulls/{num}/comments"),
        )
        result = new_comments(items, load_replied(args.state))
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


if __name__ == "__main__":
    sys.exit(main())
