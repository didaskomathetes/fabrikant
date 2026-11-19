"""Step: issue — native GitHub-Issue-Operationen (kein LLM, kein Shell/gh).

Ersetzt `shell` + `gh` in Triage-/Sweep-Loops (ADR 0001 §5): Werte gehen als
JSON-Payload an die REST-API, nie durch eine Shell → keine Injection über
Issue-/LLM-Text. Auth: GITHUB_TOKEN (Developer-Identity, ADR 0010); das Token
steht nie in Output/Summary und wird zusätzlich vom Secret-Masking erfasst.

Step-Def:
    type: issue
    action: create | edit | label | close | list | block
    repo: owner/name
    number: {issue_number}   # Pflicht außer bei create/list
    title: ...               # create (Pflicht), edit (optional)
    body: ...                # create/edit; edit + append: true hängt an den Bestand an
    labels: [a, b]           # create/label: hinzufügen (Liste oder "a,b")
    remove_labels: [c]       # label: entfernen (fehlende Labels sind kein Fehler)
    state: open | closed     # edit (optional)
    comment: ...             # close: Begründung als Kommentar vor dem Schließen
    state_reason: ...        # close: completed | not_planned | duplicate (leer = GitHub-Default completed)
    duplicate_of: "#12"      # close: als Duplikat schließen + Blocker-Relationen auf #12 umhängen (ADR 0031)
    emit: var                # create: Zielvariable der neuen Nummer (Default issue_number)
    inherit_labels: [p0, p1] # create: diese Labels übernehmen, sofern Issue `number` sie trägt
    append: true             # edit: body an bestehenden Body anhängen
    unless_contains: marker  # edit+append: No-Op, wenn der Body den Marker schon enthält
    only_if: {label: [a, b]} # nur ausführen, wenn JEDE Variable einen der Werte hat (sonst No-Op)
    skip_if_empty: [var, ...] # No-Op, wenn alle genannten Variablen leer sind
    # list (ADR 0031): andere Issues als Prompt-Kontext (Duplikat-/Blocker-Erkennung)
    state: open | closed     # list: welche Issues (Default open)
    limit: 100               # list: max. Anzahl, zuletzt aktualisierte zuerst
    emit: open_issues        # list: Zielvariable (Default issues); eine Zeile pro Issue
    # block (ADR 0031): native "blocked by"-Dependencies an Issue `number` setzen
    blocked_by: "#12, #14"   # Referenzen (#N oder N, Komma/Leerzeichen); Fremdtext wird ignoriert
Alle String-Felder sind {vars}-substituiert. `create` setzt `issue_number`
(+ `issue_url`; mit `emit` nur die Nummer in `emit`) im Variable-Store, `list` die `emit`-Variable, `block` `blockers`
(tatsächlich verknüpfte Blocker, z. B. "#12, #14").
"""

from __future__ import annotations

import contextlib
import re
import urllib.error
import urllib.parse
from collections.abc import Iterable, Mapping
from typing import cast

from ..context import _cond_matches, _substitute
from ..failures import ConfigError, Failure, classify_http, failed
from ..gitops import _gh_api, _gh_dependencies, _gh_token
from ..i18n import t
from ..runmodel import FailureKind
from .pr import _normalize_labels

ACTIONS = ("create", "edit", "label", "close", "list", "block")
_NUMBERLESS = ("create", "list")  # Actions ohne Pflicht-`number`
_API = "https://api.github.com/repos"
_STATES = ("open", "closed")
_CLOSE_REASONS = ("completed", "not_planned", "duplicate")
_PER_PAGE = 100  # GitHub-Maximum für Listen-Endpoints
LIST_LIMIT = 100  # list: Default-Obergrenze (Prompt-Größe bleibt bounded)
LIST_MAX_PAGES = 5  # list: harte Obergrenze der Pagination (≤ 500 Issues)
LIST_BODY_CHARS = 200  # list: Body-Auszug pro Issue
# block: höchstens 5 blocked_by (dependencies-Prompt, ADR 0031); mehr ist LLM-Rauschen.
# Die Voraussetzung (`prerequisite`) kommt separat dazu und fällt nie unter das Limit.
MAX_BLOCKERS = 5
_REF_RE = re.compile(r"#?(\d+)")


def _labels(value: object, ctx: Mapping[str, object]) -> list[str]:
    """Label-Liste aus YAML-Liste oder Komma-String (substituiert, ohne Leere, dedupliziert).

    Skalare (z. B. `labels: 42`) gelten als ein Label statt zu crashen.
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
    """Gescheiterte Issue-Action; ohne Angabe ``unknown`` — die meisten Werte (number, title, state, …)
    kommen per Substitution aus dem Run-Kontext (oft LLM-Output), nicht aus der Spec selbst."""
    return failed(f"issue: {msg}", failure or Failure())


def _skip_reason(ctx: dict, step: dict) -> str:
    """Grund für den No-Op (`only_if` unerfüllt / `skip_if_empty` alle leer) oder ''."""
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
    """Führt eine Issue-Action aus; Fehler → ok=False (kein Token im Text)."""
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
    token = _gh_token("dev")
    try:
        return _HANDLERS[action](ctx, step, repo, number, token)
    except urllib.error.HTTPError as e:
        detail = ""
        with contextlib.suppress(Exception):  # Body-Read ist best-effort
            detail = e.read().decode()[:300]
        return _fail(f"{action} fehlgeschlagen (HTTP {e.code}): {detail}", classify_http(e.code, e.headers))


def _text(step: dict, key: str, ctx: dict) -> str | None:
    val = step.get(key)
    return None if val is None else _substitute(str(val), ctx)


def _inherited_labels(step: dict, repo: str, number: str, token: str, ctx: dict) -> list[str]:
    """`inherit_labels`: die gelisteten Labels, die Issue `number` trägt (z. B. Priorität
    für ein abgeleitetes Issue) — bewusst eine Whitelist, damit Status-Labels wie
    `ready for development` nie mitwandern."""
    wanted = _labels(step.get("inherit_labels"), ctx)
    if not wanted or not number.isdigit():
        return []
    have = {
        str(lbl.get("name", "")) for lbl in _gh_api("GET", f"{_API}/{repo}/issues/{number}", token).get("labels") or []
    }
    return [w for w in wanted if w in have]


def _create(ctx: dict, step: dict, repo: str, number: str, token: str) -> dict:
    title = (_text(step, "title", ctx) or "").strip()
    if not title:
        return _fail(t("steps.issue.create_title_missing"))
    payload: dict = {"title": title, "body": _text(step, "body", ctx) or ""}
    labels = _labels(step.get("labels"), ctx) + _inherited_labels(step, repo, number, token, ctx)
    if labels:
        payload["labels"] = list(dict.fromkeys(labels))
    res = _gh_api("POST", f"{_API}/{repo}/issues", token, payload)
    emit = str(step.get("emit") or "issue_number")
    ctx[emit] = str(res["number"])
    if emit == "issue_number" and res.get("html_url"):
        ctx["issue_url"] = res["html_url"]
    return {"ok": True, "summary": f"Issue #{res['number']} erstellt {res.get('html_url', '')}".strip()}


def _edit(ctx: dict, step: dict, repo: str, number: str, token: str) -> dict:
    url = f"{_API}/{repo}/issues/{number}"
    payload: dict = {}
    title = _text(step, "title", ctx)
    if title is not None:
        payload["title"] = title
    state = _text(step, "state", ctx)
    if state is not None:
        state = state.strip().lower()
        if state not in _STATES:
            return _fail(t("steps.issue.state_invalid_allowed", state=state, v2=", ".join(_STATES)))
        payload["state"] = state
    body = _text(step, "body", ctx)
    if body is not None:
        if step.get("append"):
            current = _gh_api("GET", url, token).get("body") or ""
            marker = _text(step, "unless_contains", ctx)
            if marker and marker in current:
                return {"ok": True, "summary": t("steps.issue.issue_unchanged_marker_already_present", number=number)}
            body = f"{current}\n\n{body}" if current else body
        payload["body"] = body
    if not payload:
        return _fail(t("steps.issue.edit_nothing_to_change_title"))
    _gh_api("PATCH", url, token, payload)
    return {"ok": True, "summary": t("steps.issue.issue_changed", number=number, v2=", ".join(payload))}


def _label(ctx: dict, step: dict, repo: str, number: str, token: str) -> dict:
    add = _labels(step.get("labels"), ctx)
    remove = _labels(step.get("remove_labels"), ctx)
    if not add and not remove:
        return _fail(t("steps.issue.label_labels_or_remove_labels"))
    base = f"{_API}/{repo}/issues/{number}/labels"
    if add:  # GitHub legt unbekannte Labels beim Hinzufügen automatisch an
        _gh_api("POST", base, token, {"labels": add})
    for name in remove:
        try:
            _gh_api("DELETE", f"{base}/{urllib.parse.quote(name, safe='')}", token)
        except urllib.error.HTTPError as e:
            if e.code != 404:  # Label nicht (mehr) am Issue → idempotent
                raise
    return {"ok": True, "summary": f"Issue #{number} Labels +{add} -{remove}"}


def _close(ctx: dict, step: dict, repo: str, number: str, token: str) -> dict:
    reason = (_text(step, "state_reason", ctx) or "").strip().lower()
    if reason and reason not in _CLOSE_REASONS:
        return _fail(t("steps.issue.state_reason_invalid_allowed", reason=reason, v2=", ".join(_CLOSE_REASONS)))
    originals = _refs(_text(step, "duplicate_of", ctx) or "", number)
    payload: dict = {"state": "closed"}
    moved: list[str] = []
    if originals:
        original = _gh_api("GET", f"{_API}/{repo}/issues/{originals[0]}", token)
        if "pull_request" in original:
            return _fail(t("steps.issue.close_duplicate_of_is_a", v1=originals[0]))
        payload.update(state_reason="duplicate", duplicate_issue_id=original["id"])
        # Vor dem Schließen: schlägt das Umhängen fehl, bleibt das Issue offen.
        moved = _move_dependents(repo, number, original, token)
    elif reason:
        payload["state_reason"] = reason
    comment = (_text(step, "comment", ctx) or "").strip()
    if comment:
        _gh_api("POST", f"{_API}/{repo}/issues/{number}/comments", token, {"body": comment})
    _gh_api("PATCH", f"{_API}/{repo}/issues/{number}", token, payload)
    summary = f"Issue #{number} geschlossen" + (f" ({payload['state_reason']})" if "state_reason" in payload else "")
    if moved:
        summary += t("steps.issue.blockers_moved_to", v1=originals[0], v2=", ".join(moved))
    return {"ok": True, "summary": summary}


def _move_dependents(repo: str, number: str, original: dict, token: str) -> list[str]:
    """Issues, die `number` blockiert, werden stattdessen vom Original blockiert.

    Sonst hingen sie an einem als `duplicate` geschlossenen Blocker fest (der zählt
    nicht als erledigt, ADR 0031). Idempotent: bestehende Relationen bleiben.
    """
    me = _gh_api("GET", f"{_API}/{repo}/issues/{number}", token)
    moved = []
    for dep in _gh_dependencies(repo, number, token, "blocking"):
        dep_no = dep["number"]
        base = f"{_API}/{repo}/issues/{dep_no}/dependencies/blocked_by"
        # Hing das Original selbst am Duplikat, wird die Relation nur gelöst
        # (keine Selbst-Blockade).
        if dep_no != original["number"]:
            if original["number"] not in {b.get("number") for b in _gh_dependencies(repo, dep_no, token)}:
                _gh_api("POST", base, token, {"issue_id": original["id"]})
            moved.append(f"#{dep_no}")
        _gh_api("DELETE", f"{base}/{me['id']}", token)
    return moved


def _issue_line(it: dict) -> str:
    """Kompakte Prompt-Zeile: `#N [labels] Titel — Body-Auszug` (einzeilig)."""
    labels = ", ".join(str(lbl.get("name", "")) for lbl in it.get("labels") or [] if isinstance(lbl, dict))
    body = " ".join((it.get("body") or "").split())[:LIST_BODY_CHARS]
    line = f"#{it['number']}" + (f" [{labels}]" if labels else "") + f" {it.get('title', '')}"
    return f"{line} — {body}" if body else line


def _list(ctx: dict, step: dict, repo: str, number: str, token: str) -> dict:
    """Andere Issues (ohne PRs und ohne Issue `number`) als Textliste in `emit`."""
    state = (_text(step, "state", ctx) or "open").strip().lower()
    if state not in _STATES:
        return _fail(t("steps.issue.state_invalid_allowed", state=state, v2=", ".join(_STATES)))
    limit = max(1, int(step.get("limit", LIST_LIMIT)))
    query = {"state": state, "per_page": _PER_PAGE, "sort": "updated", "direction": "desc"}
    labels = _labels(step.get("labels"), ctx)
    if labels:
        query["labels"] = ",".join(labels)
    lines: list[str] = []
    for page in range(1, LIST_MAX_PAGES + 1):
        url = f"{_API}/{repo}/issues?{urllib.parse.urlencode({**query, 'page': page})}"
        items = cast(list[dict], _gh_api("GET", url, token) or [])
        lines += [_issue_line(it) for it in items if "pull_request" not in it and str(it["number"]) != number]
        if len(lines) >= limit or len(items) < _PER_PAGE:
            break
    var = str(step.get("emit") or "issues")
    ctx[var] = "\n".join(lines[:limit])
    return {"ok": True, "summary": f"{min(len(lines), limit)} Issues ({state}) → {var}"}


def _refs(text: str, own: str) -> list[int]:
    """Issue-Nummern aus `#12, 14 #15`; Tokens, die keine reine Referenz sind, und das
    eigene Issue werden ignoriert (LLM-Text ist untrusted). Reihenfolge bleibt erhalten."""
    out: list[int] = []
    for tok in re.split(r"[\s,;]+", text):
        m = _REF_RE.fullmatch(tok)
        if m and m.group(1) != own and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return out


def _block(ctx: dict, step: dict, repo: str, number: str, token: str) -> dict:
    """Setzt native "blocked by"-Dependencies (ADR 0031), idempotent.

    `blocked_by` (LLM-Text) ist auf MAX_BLOCKERS begrenzt, `prerequisite` (die neu
    angelegte Voraussetzung) kommt immer zusätzlich dazu.
    Nicht existente Referenzen, PRs und geschlossene Issues werden übersprungen
    (blockieren nichts). Andere API-Fehler (z. B. 422 bei Zyklen) brechen den Step
    ab — fail closed: der Refine-Run labelt das Issue dann nicht.
    """
    blocked_by = _refs(_text(step, "blocked_by", ctx) or "", number)
    dropped = blocked_by[MAX_BLOCKERS:]
    refs = blocked_by[:MAX_BLOCKERS]
    refs += [n for n in _refs(_text(step, "prerequisite", ctx) or "", number) if n not in refs]
    if not refs:
        return {"ok": True, "summary": t("steps.issue.issue_no_blockers", number=number)}
    existing = {b.get("number") for b in _gh_dependencies(repo, number, token)}
    linked, skipped = [], [t("steps.issue.over_limit", n=n, v2=MAX_BLOCKERS) for n in dropped]
    for ref in refs:
        if ref in existing:
            linked.append(f"#{ref}")
            continue
        try:
            blocker = _gh_api("GET", f"{_API}/{repo}/issues/{ref}", token)
        except urllib.error.HTTPError as e:
            if e.code not in (404, 410):  # gelöscht/nicht vorhanden → halluzinierte Referenz
                raise
            skipped.append(t("steps.issue.not_found", ref=ref))
            continue
        if "pull_request" in blocker or blocker.get("state") != "open":
            skipped.append(f"#{ref} ({'PR' if 'pull_request' in blocker else 'geschlossen'})")
            continue
        _gh_api("POST", f"{_API}/{repo}/issues/{number}/dependencies/blocked_by", token, {"issue_id": blocker["id"]})
        linked.append(f"#{ref}")
    ctx["blockers"] = ", ".join(linked)
    summary = f"Issue #{number} blocked by: {', '.join(linked) or '—'}"
    if skipped:
        summary += t("steps.issue.skipped", v1=", ".join(skipped))
    return {"ok": True, "summary": summary}


_HANDLERS = {"create": _create, "edit": _edit, "label": _label, "close": _close, "list": _list, "block": _block}
