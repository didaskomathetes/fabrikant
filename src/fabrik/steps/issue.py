"""Step: issue — native Issue-Operationen über den Hosting-Provider (ADR 0001 §5, 0061).

Ersetzt `shell` + `gh` in Triage-/Sweep-Loops (ADR 0001 §5): Werte gehen als
JSON-Payload an die REST-API, nie durch eine Shell → keine Injection über
Issue-/LLM-Text. Der Step spricht den Hosting-Provider (ADR 0062), nicht die
GitHub-API direkt: GitHub und GitLab laufen über denselben Pfad.

Auth: Developer-Identity (ADR 0010); das Token steht nie in Output/Summary und
wird zusätzlich vom Secret-Masking erfasst.

GitHub-spezifische Features (native Issue-Dependencies, ADR 0031): `block` und
`duplicate_of` nutzen native "blocked by"-Relationen, die GitLab nicht hat —
dort werfen sie `HostingNotSupported` (fail-closed).

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
    state_reason: ...        # close: completed | not_planned | duplicate (leer = Host-Default)
    duplicate_of: "#12"      # close: als Duplikat schließen + Blocker-Relationen auf #12 umhängen (ADR 0031, GitHub)
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

from ..context import _cond_matches, _substitute
from ..failures import ConfigError, Failure, classify_http, failed
from ..hosting import Provider, get_provider
from ..runmodel import FailureKind
from .pr import _normalize_labels

ACTIONS = ("create", "edit", "label", "close", "list", "block")
_NUMBERLESS = ("create", "list")  # Actions ohne Pflicht-`number`
_STATES = ("open", "closed")
_CLOSE_REASONS = ("completed", "not_planned", "duplicate")
_PER_PAGE = 100  # Host-Maximum für Listen-Endpoints
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
        return "only_if nicht erfüllt"
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
        return {"ok": True, "summary": f"issue übersprungen ({skipped})"}
    action = str(step.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return _fail(f"action '{action}' ungültig (erlaubt: {', '.join(ACTIONS)})", Failure(FailureKind.ENVIRONMENT))
    repo = _substitute(str(step.get("repo") or ""), ctx).strip()
    if not repo:
        raise ConfigError("issue: 'repo: owner/name' fehlt")
    number = _substitute(str(step.get("number") or ""), ctx).strip() or str(ctx.get("issue_number") or "").strip()
    if action not in _NUMBERLESS and not number.isdigit():
        return _fail(f"number nicht auflösbar (number: {number!r})")
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
    """`inherit_labels`: die gelisteten Labels, die Issue `number` trägt (z. B. Priorität
    für ein abgeleitetes Issue) — bewusst eine Whitelist, damit Status-Labels wie
    `ready for development` nie mitwandern."""
    wanted = _labels(step.get("inherit_labels"), ctx)
    if not wanted or not number.isdigit():
        return []
    have = set(provider.get_issue(repo, int(number)).labels)
    return [w for w in wanted if w in have]


def _create(ctx: dict, step: dict, repo: str, number: str, provider: Provider) -> dict:
    title = (_text(step, "title", ctx) or "").strip()
    if not title:
        return _fail("create: 'title' fehlt")
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
            return _fail(f"state '{state}' ungültig (erlaubt: {', '.join(_STATES)})")
    body = _text(step, "body", ctx)
    if body is not None and step.get("append"):
        current = provider.get_issue(repo, int(number)).body
        marker = _text(step, "unless_contains", ctx)
        if marker and marker in current:
            return {"ok": True, "summary": f"Issue #{number} unverändert (Marker bereits vorhanden)"}
        body = f"{current}\n\n{body}" if current else body
    if title is None and body is None and state is None:
        return _fail("edit: nichts zu ändern (title/body/state fehlen)")
    provider.edit_issue(repo, int(number), title=title, body=body, state=state)
    changed = [k for k, v in (("title", title), ("body", body), ("state", state)) if v is not None]
    return {"ok": True, "summary": f"Issue #{number} geändert ({', '.join(changed)})"}


def _label(ctx: dict, step: dict, repo: str, number: str, provider: Provider) -> dict:
    add = _labels(step.get("labels"), ctx)
    remove = _labels(step.get("remove_labels"), ctx)
    if not add and not remove:
        return _fail("label: 'labels' oder 'remove_labels' fehlt")
    if add:  # GitHub legt unbekannte Labels beim Hinzufügen automatisch an
        provider.add_labels(repo, int(number), add)
    for name in remove:
        provider.remove_label(repo, int(number), name)  # 404 → idempotent (Provider)
    return {"ok": True, "summary": f"Issue #{number} Labels +{add} -{remove}"}


def _close(ctx: dict, step: dict, repo: str, number: str, provider: Provider) -> dict:
    reason = (_text(step, "state_reason", ctx) or "").strip().lower()
    if reason and reason not in _CLOSE_REASONS:
        return _fail(f"state_reason '{reason}' ungültig (erlaubt: {', '.join(_CLOSE_REASONS)})")
    originals = _refs(_text(step, "duplicate_of", ctx) or "", number)
    duplicate_id: int | None = None
    moved: list[str] = []
    if originals:
        original = provider.get_issue(repo, originals[0])
        if original.is_pr:
            return _fail(f"close: duplicate_of #{originals[0]} ist ein PR")
        duplicate_id = int(original.raw["id"])
        # Vor dem Schließen: schlägt das Umhängen fehl, bleibt das Issue offen.
        moved = _move_dependents(provider, repo, number, original)
        reason = "duplicate"
    comment = (_text(step, "comment", ctx) or "").strip()
    if comment:
        provider.create_comment(repo, int(number), comment)
    # GitHub: `duplicate_issue_id` setzt die native Duplikat-Relation (ADR 0031);
    # GitLab ignoriert das Feld (kein Duplikat-Konzept, ADR 0062).
    provider.edit_issue(repo, int(number), state="closed", state_reason=reason or None, duplicate_issue_id=duplicate_id)
    summary = f"Issue #{number} geschlossen" + (f" ({reason})" if reason else "")
    if moved:
        summary += f"; Blocker umgehängt auf #{originals[0]}: {', '.join(moved)}"
    return {"ok": True, "summary": summary}


def _move_dependents(provider: Provider, repo: str, number: str, original) -> list[str]:
    """Issues, die `number` blockiert, werden stattdessen vom Original blockiert.

    Sonst hingen sie an einem als `duplicate` geschlossenen Blocker fest (der zählt
    nicht als erledigt, ADR 0031). Idempotent: bestehende Relationen bleiben.
    """
    me = provider.get_issue(repo, int(number))
    moved = []
    for dep in provider.issue_dependencies(repo, int(number), kind="blocking"):
        dep_no = dep.number
        # Hing das Original selbst am Duplikat, wird die Relation nur gelöst
        # (keine Selbst-Blockade).
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
    """Andere Issues (ohne PRs und ohne Issue `number`) als Textliste in `emit`."""
    state = (_text(step, "state", ctx) or "open").strip().lower()
    if state not in _STATES:
        return _fail(f"state '{state}' ungültig (erlaubt: {', '.join(_STATES)})")
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
    """Issue-Nummern aus `#12, 14 #15`; Tokens, die keine reine Referenz sind, und das
    eigene Issue werden ignoriert (LLM-Text ist untrusted). Reihenfolge bleibt erhalten."""
    out: list[int] = []
    for tok in re.split(r"[\s,;]+", text):
        m = _REF_RE.fullmatch(tok)
        if m and m.group(1) != own and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return out


def _block(ctx: dict, step: dict, repo: str, number: str, provider: Provider) -> dict:
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
        return {"ok": True, "summary": f"Issue #{number}: keine Blocker"}
    existing = {b.number for b in provider.issue_dependencies(repo, int(number))}
    linked, skipped = [], [f"#{n} (über Limit {MAX_BLOCKERS})" for n in dropped]
    for ref in refs:
        if ref in existing:
            linked.append(f"#{ref}")
            continue
        try:
            blocker = provider.get_issue(repo, ref)
        except urllib.error.HTTPError as e:
            if e.code not in (404, 410):  # gelöscht/nicht vorhanden → halluzinierte Referenz
                raise
            skipped.append(f"#{ref} (nicht gefunden)")
            continue
        if blocker.is_pr or blocker.state != "open":
            skipped.append(f"#{ref} ({'PR' if blocker.is_pr else 'geschlossen'})")
            continue
        provider.add_issue_dependency(repo, int(number), blocker_id=int(blocker.raw["id"]))
        linked.append(f"#{ref}")
    ctx["blockers"] = ", ".join(linked)
    summary = f"Issue #{number} blocked by: {', '.join(linked) or '—'}"
    if skipped:
        summary += f"; übersprungen: {', '.join(skipped)}"
    return {"ok": True, "summary": summary}


_HANDLERS = {"create": _create, "edit": _edit, "label": _label, "close": _close, "list": _list, "block": _block}
