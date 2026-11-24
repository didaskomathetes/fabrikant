"""The one status comment per relay issue: current state for humans + state for the dispatcher."""

from __future__ import annotations

import json
import re

from .model import AUDIT_SECTION, RESULT_SECTION, Handoff, Status, StatusState, TrailRef, format_time

STATUS_MARKER = "<!-- fabrikant-relay-status -->"
_STATE_PREFIX = "<!-- fabrikant-relay-state "
_STATE_RE = re.compile(re.escape(_STATE_PREFIX) + r"(\{.*?\}) -->", re.S)
_STATUS_ICON = {Status.ACTIVE: "🔄", Status.BLOCKED: "⛔", Status.DONE: "✅"}
# GitHub comments are limited to 65,536 characters; result/audit remain summaries.
SECTION_MAX_CHARS = 4000


def _excerpt(text: str) -> str:
    text = text.strip()
    return text if len(text) <= SECTION_MAX_CHARS else text[:SECTION_MAX_CHARS].rstrip() + " …"


def _archive_cell(ref: TrailRef) -> str:
    where = f"[{ref.method}]({ref.uri})" if ref.uri.startswith("https://") else f"{ref.method} `{ref.uri}`"
    return f"`{ref.name}` · {where} · removed from the branch"


# Block in the PR body pointing to the archived trail; fabrikant owns only the text between the markers.
PR_TRAIL_START = "<!-- fabrikant-relay-trail -->"
PR_TRAIL_END = "<!-- /fabrikant-relay-trail -->"
_PR_TRAIL_RE = re.compile(r"\n*" + re.escape(PR_TRAIL_START) + r".*?" + re.escape(PR_TRAIL_END) + r"\n?", re.S)


def pr_trail_block(repo: str, issue: int, trail_dir: str, ref: TrailRef, ref_name: str) -> str:
    """Markdown block for the PR body: where the archived trail lives and how to verify it.

    `ref_name` is the archive's git ref (only for `git-ref`, otherwise empty). The last line of
    `AUDIT.md` in the archive carries the trail hash — the verification command shows exactly it."""
    lines = [PR_TRAIL_START, "---", f"**Relay trail** (archived after the audit, not part of this diff) · #{issue}", ""]
    if ref.method == "git-ref" and ref.uri.startswith("https://"):
        report = f"https://github.com/{repo}/blob/{ref.commit}/{trail_dir}/AUDIT.md"
        lines += [
            f"- Archive: [`{ref.name}`]({ref.uri}) · ref `{ref_name}` @ `{ref.commit[:12]}`",
            f"- Audit report: [AUDIT.md]({report})",
            f"- Trail sha256: `{ref.sha256}`",
            f"- Check: `git fetch origin {ref_name} && git show FETCH_HEAD:{trail_dir}/AUDIT.md | tail -n 1`",
        ]
    else:
        lines += [f"- Archive: `{ref.name}` · {ref.method} `{ref.uri}`", f"- Trail sha256: `{ref.sha256}`"]
    return "\n".join([*lines, PR_TRAIL_END])


def with_pr_trail(body: str, block: str | None) -> str:
    """PR body with exactly one trail block (`block`) or none (`None`); remaining text stays."""
    rest = _PR_TRAIL_RE.sub("\n", body or "").rstrip()
    if block is None:
        return rest + "\n" if rest else ""
    return f"{rest}\n\n{block}\n" if rest else block + "\n"


def parse_state(body: str) -> StatusState:
    """State from a status comment; missing/broken → default state."""
    m = _STATE_RE.search(body)
    if not m:
        return StatusState()
    try:
        return StatusState.from_dict(json.loads(m.group(1)))
    except json.JSONDecodeError:
        return StatusState()


def render(
    repo: str, branch: str, handoff_path: str, handoff: Handoff | None, state: StatusState, max_sessions: int
) -> str:
    """Markdown of the status comment (edited in place on every change)."""
    base = f"https://github.com/{repo}"
    audit_path = handoff_path.rsplit("/", 1)[0] + "/AUDIT.md"
    # Trail archived (removed from the branch): links to the archived commit instead of the branch.
    # Only `git-ref` has a permanent, linkable commit; `local` is development/tests only,
    # there the links point to the (empty) branch after the strip — acceptable, no behavior change.
    trail_ref = state.archive.commit if state.archive and state.archive.method == "git-ref" else branch
    lines = [STATUS_MARKER, "### fabrikant relay — status", ""]
    if handoff is None:
        lines.append("_No session yet._")
    else:
        when = format_time(handoff.last_session) or "–"
        next_run = format_time(handoff.not_before) if handoff.not_before else "next dispatcher tick"
        rows = [
            ("Status", f"{_STATUS_ICON[handoff.status]} **{handoff.status}**"),
            ("Session", f"{handoff.session} / {max_sessions} (last: {when})"),
            ("Progress", handoff.progress or "–"),
            ("Verify", f"`{handoff.verify_result}`" if handoff.verify_result else "–"),
            ("Next session", next_run if handoff.status is Status.ACTIVE else "–"),
            ("Branch", f"[`{branch}`]({base}/tree/{branch}) · [handoff]({base}/blob/{trail_ref}/{handoff_path})"),
            ("Last commit", f"`{state.last_commit}`" if state.last_commit else "–"),
            ("Audit", f"{state.audit} · [AUDIT.md]({base}/blob/{trail_ref}/{audit_path})" if state.audit else "–"),
        ]
        if state.trail_sha256:
            rows.append(("Trail sha256", f"`{state.trail_sha256}`"))
        if state.archive:
            rows.append(("Trail archive", _archive_cell(state.archive)))
        lines += ["| | |", "|---|---|", *(f"| {k} | {v} |" for k, v in rows)]
        if handoff.status is Status.BLOCKED and handoff.blocked_reason:
            lines += [
                "",
                f"**Blocked:** {handoff.blocked_reason}",
                "",
                "_Remove the `relay: blocked` label to continue._",
            ]
        for title, name in (("Result", RESULT_SECTION), ("Audit", AUDIT_SECTION)):
            if text := handoff.section(name):
                lines += ["", f"#### {title}", _excerpt(text)]
    if state.trail_warning:
        lines += ["", f"⚠️ **Trail:** {state.trail_warning}"]
    if state.note:
        lines += ["", f"**Dispatcher:** {state.note}"]
    lines += ["", f"{_STATE_PREFIX}{json.dumps(state.to_dict(), sort_keys=True)} -->"]
    return "\n".join(lines)
