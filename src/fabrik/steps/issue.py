"""Step: issue — native issue operations via the hosting provider (ADR 0001 §5, 0061).

Replaces `shell` + `gh` in triage/sweep loops (ADR 0001 §5): values go as a
JSON payload to the REST API, never through a shell → no injection via
issue/LLM text. The step talks to the hosting provider (ADR 0062), not the
GitHub API directly: GitHub and GitLab use the same path.

Auth: developer identity (ADR 0010); the token never appears in output/summary and
is additionally covered by secret masking.

GitHub-specific features (native issue dependencies, ADR 0031): `block` and
`duplicate_of` use native "blocked by" relations, which GitLab does not have —
there they raise `HostingNotSupported` (fail-closed).

Step def:
    type: issue
    action: create | edit | label | close | list | block
    repo: owner/name
    number: {issue_number}   # required except for create/list
    title: ...               # create (required), edit (optional)
    body: ...                # create/edit; edit + append: true appends to the existing body
    labels: [a, b]           # create/label: add (list or "a,b")
    remove_labels: [c]       # label: remove (missing labels are no error)
    state: open | closed     # edit (optional)
    comment: ...             # close: reason as a comment before closing
    state_reason: ...        # close: completed | not_planned | duplicate (empty = host default)
    duplicate_of: "#12"      # close: close as duplicate + re-hang blocker relations to #12 (ADR 0031, GitHub)
    emit: var                # create: target variable of the new number (default issue_number)
    inherit_labels: [p0, p1] # create: take over these labels if issue `number` carries them
    append: true             # edit: append body to the existing body
    unless_contains: marker  # edit+append: no-op if the body already contains the marker
    only_if: {label: [a, b]} # run only if EVERY variable has one of the values (otherwise no-op)
    skip_if_empty: [var, ...] # no-op if all named variables are empty
    # list (ADR 0031): other issues as prompt context (duplicate/blocker detection)
    state: open | closed     # list: which issues (default open)
    limit: 100               # list: max. count, most recently updated first
    emit: open_issues        # list: target variable (default issues); one line per issue
    # block (ADR 0031): set native "blocked by" dependencies on issue `number`
    blocked_by: "#12, #14"   # references (#N or N, comma/space); foreign text is ignored
All string fields are {vars}-substituted. `create` sets `issue_number`
(+ `issue_url`; with `emit` only the number in `emit`) in the variable store, `list` the `emit` variable, `block` `blockers`
(actually linked blockers, e.g. "#12, #14").
"""

from __future__ import annotations

import contextlib
import re
import urllib.error
import urllib.parse
from collections.abc import Iterable, Mapping

from ..context import _cond_matches, _substitute
from ..failures import ConfigError, Failure, classify_http, failed
from ..hosting import Provider, get_provider
from ..i18n import t
from ..runmodel import FailureKind
from .pr import _normalize_labels

ACTIONS = ("create", "edit", "label", "close", "list", "block")
_NUMBERLESS = ("create", "list")  # Actions ohne Pflicht-`number`
_STATES = ("open", "closed")
_CLOSE_REASONS = ("completed", "not_planned", "duplicate")
_PER_PAGE = 100  # host maximum for list endpoints
LIST_LIMIT = 100  # list: default upper bound (prompt size stays bounded)
LIST_MAX_PAGES = 5  # list: harte Obergrenze der Pagination (≤ 500 Issues)
LIST_BODY_CHARS = 200  # list: Body-Auszug pro Issue
# block: at most 5 blocked_by (dependencies prompt, ADR 0031); more is LLM noise.
# The prerequisite (`prerequisite`) is added separately and never falls under the limit.
MAX_BLOCKERS = 5
_REF_RE = re.compile(r"#?(\d+)")


def _labels(value: object, ctx: Mapping[str, object]) -> list[str]:
    """Label list from a YAML list or comma string (substituted, without empties, deduplicated).

    Scalars (e.g. `labels: 42`) count as one label instead of crashing.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items: Iterable[object] = value.split(",")
    elif isinstance(value, Iterable) and not isinstance(value, Mapping):
        items = value
    else:
        items = [value]
    return _normalize_labels(_substitute(str(i), ctx) for i in items)


def _fail(msg: str, failure: Failure | None = None) -> dict:
    """Failed issue action; without specification ``unknown`` — most values (number, title, state, …)
    come by substitution from the run context (often LLM output), not from the spec itself."""
    return failed(f"issue: {msg}", failure or Failure())


def _skip_reason(ctx: dict, step: dict) -> str:
    """Reason for the no-op (`only_if` unmet / `skip_if_empty` all empty) or ''."""
    only_if = step.get("only_if")
    if only_if and not _cond_matches(only_if, ctx):
        return t("steps.issue.only_if_not_satisfied")
    names = step.get("skip_if_empty")
    if names:
        names = [names] if isinstance(names, str) else names
        if not any(str(ctx.get(n) or "").strip() for n in names):
            return f"{', '.join(names)} leer"
    return ""


def _step_issue(ctx: dict, step: dict) -> dict:
    """Executes an issue action; error → ok=False (no token in the text)."""
    skipped = _skip_reason(ctx, step)
    if skipped:
        return {"ok": True, "summary": t("steps.issue.issue_skipped", skipped=skipped)}
    action = str(step.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return _fail(
            t("steps.issue.action_invalid_allowed", action=action, v2=", ".join(ACTIONS)),
            Failure(FailureKind.ENVIRONMENT),
        )
    repo = _substitute(str(step.get("repo") or ""), ctx).strip()
    if not repo:
        raise ConfigError(t("steps.issue.issue_repo_owner_name_missing"))
    number = _substitute(str(step.get("number") or ""), ctx).strip() or str(ctx.get("issue_number") or "").strip()
    if action not in _NUMBERLESS and not number.isdigit():
        return _fail(t("steps.issue.number_cannot_be_resolved_number", number=number))
    provider = get_provider(repo)
    try:
        return _HANDLERS[action](ctx, step, repo, number, provider)
    except urllib.error.HTTPError as e:
        detail = ""
        with contextlib.suppress(Exception):  # Body-Read ist best-effort
            detail = e.read().decode()[:300]
        return _fail(f"{action} fehlgeschlagen (HTTP {e.code}): {detail}", classify_http(e.code, e.headers))


def _text(step: dict, key: str, ctx: dict) -> str | None:
    val = step.get(key)
    return None if val is None else _substitute(str(val), ctx)


def _inherited_labels(step: dict, repo: str, number: str, provider: Provider, ctx: dict) -> list[str]:
    """`inherit_labels`: the listed labels that issue `number` carries (e.g. priority
    for a derived issue) — deliberately a whitelist, so status labels like
    `ready for development` never migrate along."""
    wanted = _labels(step.get("inherit_labels"), ctx)
    if not wanted or not number.isdigit():
        return []
    have = set(provider.get_issue(repo, int(number)).labels)
    return [w for w in wanted if w in have]


def _create(ctx: dict, step: dict, repo: str, number: str, provider: Provider) -> dict:
    title = (_text(step, "title", ctx) or "").strip()
    if not title:
        return _fail(t("steps.issue.create_title_missing"))
    body = _text(step, "body", ctx) or ""
    labels = _labels(step.get("labels"), ctx) + _inherited_labels(step, repo, number, provider, ctx)
    issue = provider.create_issue(repo, title, body, list(dict.fromkeys(labels)) if labels else None)
    emit = str(step.get("emit") or "issue_number")
    ctx[emit] = str(issue.number)
    if emit == "issue_number" and issue.url:
        ctx["issue_url"] = issue.url
    return {"ok": True, "summary": f"Issue #{issue.number} erstellt {issue.url}".strip()}


def _edit(ctx: dict, step: dict, repo: str, number: str, provider: Provider) -> dict:
    title = _text(step, "title", ctx)
    state = _text(step, "state", ctx)
    if state is not None:
        state = state.strip().lower()
        if state not in _STATES:
            return _fail(t("steps.issue.state_invalid_allowed", state=state, v2=", ".join(_STATES)))
    body = _text(step, "body", ctx)
    if body is not None and step.get("append"):
        current = provider.get_issue(repo, int(number)).body
        marker = _text(step, "unless_contains", ctx)
        if marker and marker in current:
            return {"ok": True, "summary": t("steps.issue.issue_unchanged_marker_already_present", number=number)}
        body = f"{current}\n\n{body}" if current else body
    if title is None and body is None and state is None:
        return _fail(t("steps.issue.edit_nothing_to_change_title"))
    provider.edit_issue(repo, int(number), title=title, body=body, state=state)
    changed = [k for k, v in (("title", title), ("body", body), ("state", state)) if v is not None]
    return {"ok": True, "summary": t("steps.issue.issue_changed", number=number, v2=", ".join(changed))}


def _label(ctx: dict, step: dict, repo: str, number: str, provider: Provider) -> dict:
    add = _labels(step.get("labels"), ctx)
    remove = _labels(step.get("remove_labels"), ctx)
    if not add and not remove:
        return _fail(t("steps.issue.label_labels_or_remove_labels"))
    if add:  # GitHub creates unknown labels automatically when adding them
        provider.add_labels(repo, int(number), add)
    for name in remove:
        provider.remove_label(repo, int(number), name)  # 404 → idempotent (Provider)
    return {"ok": True, "summary": f"Issue #{number} Labels +{add} -{remove}"}


def _close(ctx: dict, step: dict, repo: str, number: str, provider: Provider) -> dict:
    reason = (_text(step, "state_reason", ctx) or "").strip().lower()
    if reason and reason not in _CLOSE_REASONS:
        return _fail(t("steps.issue.state_reason_invalid_allowed", reason=reason, v2=", ".join(_CLOSE_REASONS)))
    originals = _refs(_text(step, "duplicate_of", ctx) or "", number)
    duplicate_id: int | None = None
    moved: list[str] = []
    if originals:
        original = provider.get_issue(repo, originals[0])
        if original.is_pr:
            return _fail(t("steps.issue.close_duplicate_of_is_a", v1=originals[0]))
        duplicate_id = int(original.raw["id"])
        # Before closing: if re-hanging fails, the issue stays open.
        moved = _move_dependents(provider, repo, number, original)
        reason = "duplicate"
    comment = (_text(step, "comment", ctx) or "").strip()
    if comment:
        provider.create_comment(repo, int(number), comment)
    # GitHub: `duplicate_issue_id` sets the native duplicate relation (ADR 0031);
    # GitLab ignores the field (no duplicate concept, ADR 0062).
    provider.edit_issue(repo, int(number), state="closed", state_reason=reason or None, duplicate_issue_id=duplicate_id)
    summary = f"Issue #{number} geschlossen" + (f" ({reason})" if reason else "")
    if moved:
        summary += t("steps.issue.blockers_moved_to", v1=originals[0], v2=", ".join(moved))
    return {"ok": True, "summary": summary}


def _move_dependents(provider: Provider, repo: str, number: str, original) -> list[str]:
    """Issues that `number` blocks are blocked by the original instead.

    Otherwise they would be stuck on a blocker closed as `duplicate` (which does not
    count as done, ADR 0031). Idempotent: existing relations stay.
    """
    me = provider.get_issue(repo, int(number))
    moved = []
    for dep in provider.issue_dependencies(repo, int(number), kind="blocking"):
        dep_no = dep.number
        # If the original itself hung on the duplicate, the relation is only removed
        # (no self-block).
        if dep_no != original.number:
            if original.number not in {b.number for b in provider.issue_dependencies(repo, dep_no)}:
                provider.add_issue_dependency(repo, dep_no, blocker_id=original.raw["id"])
            moved.append(f"#{dep_no}")
        provider.remove_issue_dependency(repo, dep_no, blocker_id=me.raw["id"])
    return moved


def _issue_line(it) -> str:
    """Kompakte Prompt-Zeile: `#N [labels] Titel — Body-Auszug` (einzeilig)."""
    labels = ", ".join(it.labels)
    body = " ".join(it.body.split())[:LIST_BODY_CHARS]
    line = f"#{it.number}" + (f" [{labels}]" if labels else "") + f" {it.title}"
    return f"{line} — {body}" if body else line


def _list(ctx: dict, step: dict, repo: str, number: str, provider: Provider) -> dict:
    """Other issues (without PRs and without issue `number`) as a text list in `emit`."""
    state = (_text(step, "state", ctx) or "open").strip().lower()
    if state not in _STATES:
        return _fail(t("steps.issue.state_invalid_allowed", state=state, v2=", ".join(_STATES)))
    limit = max(1, int(step.get("limit", LIST_LIMIT)))
    labels = _labels(step.get("labels"), ctx)
    lines: list[str] = []
    for page in range(1, LIST_MAX_PAGES + 1):
        items = provider.list_issues(
            repo, state=state, labels=labels or None, sort="updated", direction="desc", page=page, per_page=_PER_PAGE
        )
        lines += [_issue_line(it) for it in items if not it.is_pr and str(it.number) != number]
        if len(lines) >= limit or len(items) < _PER_PAGE:
            break
    var = str(step.get("emit") or "issues")
    ctx[var] = "\n".join(lines[:limit])
    return {"ok": True, "summary": f"{min(len(lines), limit)} Issues ({state}) → {var}"}


def _refs(text: str, own: str) -> list[int]:
    """Issue numbers from `#12, 14 #15`; tokens that are not a pure reference and the
    own issue are ignored (LLM text is untrusted). Order is preserved."""
    out: list[int] = []
    for tok in re.split(r"[\s,;]+", text):
        m = _REF_RE.fullmatch(tok)
        if m and m.group(1) != own and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return out


def _block(ctx: dict, step: dict, repo: str, number: str, provider: Provider) -> dict:
    """Sets native "blocked by" dependencies (ADR 0031), idempotent.


    `blocked_by` (LLM text) is limited to MAX_BLOCKERS, `prerequisite` (the newly
    created prerequisite) is always added on top.
    Non-existent references, PRs and closed issues are skipped
    (block nothing). Other API errors (e.g. 422 on cycles) abort the step
    — fail closed: the refine run then does not label the issue.
    """
    blocked_by = _refs(_text(step, "blocked_by", ctx) or "", number)
    dropped = blocked_by[MAX_BLOCKERS:]
    refs = blocked_by[:MAX_BLOCKERS]
    refs += [n for n in _refs(_text(step, "prerequisite", ctx) or "", number) if n not in refs]
    if not refs:
        return {"ok": True, "summary": t("steps.issue.issue_no_blockers", number=number)}
    existing = {b.number for b in provider.issue_dependencies(repo, int(number))}
    linked, skipped = [], [t("steps.issue.over_limit", n=n, v2=MAX_BLOCKERS) for n in dropped]

    for ref in refs:
        if ref in existing:
            linked.append(f"#{ref}")
            continue
        try:
            blocker = provider.get_issue(repo, ref)
        except urllib.error.HTTPError as e:
            if e.code not in (404, 410):  # deleted/not present → hallucinated reference
                raise
            skipped.append(t("steps.issue.not_found", ref=ref))
            continue
        if blocker.is_pr or blocker.state != "open":
            skipped.append(f"#{ref} ({'PR' if blocker.is_pr else 'geschlossen'})")
            continue
        provider.add_issue_dependency(repo, int(number), blocker_id=int(blocker.raw["id"]))
        linked.append(f"#{ref}")
    ctx["blockers"] = ", ".join(linked)
    summary = f"Issue #{number} blocked by: {', '.join(linked) or '—'}"
    if skipped:
        summary += t("steps.issue.skipped", v1=", ".join(skipped))
    return {"ok": True, "summary": summary}


_HANDLERS = {"create": _create, "edit": _edit, "label": _label, "close": _close, "list": _list, "block": _block}
