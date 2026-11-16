"""Step: pr (GitHub-PR anlegen).

Idempotent (Duplikat-Schutz): existiert für den Head-Branch bereits ein
offener PR, wird er wiederverwendet — GitHub erlaubt ohnehin nur einen
offenen PR pro Head-Branch, der Create-Call würde also fehlschlagen.
Wichtig für Re-Runs/Resumes desselben Issues.

Leerer Head: hat `head` keine Commits vor `base` (z. B. Agent fand nichts zu
tun, `ship` war ein No-Op), lehnt GitHub den Create mit 422 ab. Der Step prüft
das vorher über die Compare-API und beendet den Run dann erfolgreich (`done`)
statt mit Fehler — Folge-Steps (Review, Merge) hätten keinen PR.

Labels: `POST /pulls` kennt keinen labels-Parameter (GitHub ignoriert ihn
stillschweigend) — sie werden danach über den Issues-Endpunkt gesetzt, auch
für wiederverwendete PRs (additiv, idempotent; heilt PRs ohne Label).

Step-Def:
    type: pr
    repo: <Pflicht, "owner/name">
    base: <optional, Default: main>
    head: <optional, Default: aktueller Branch>
    title: <optional, {vars}-substituiert>
    body: <optional, {vars}-substituiert>
    labels: <optional, Liste; z. B. [fabrik] — Review-Loops filtern oft nach Label>
"""

from __future__ import annotations

import urllib.error
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..context import _substitute
from ..failures import ConfigError
from ..gitops import _gh_api, _gh_token
from ..validation import validate_branch_name
from .git import _git


def _step_pr(ctx: dict, step: dict) -> dict:
    repo_cfg = step.get("repo")  # "owner/name"
    if not repo_cfg:
        raise ConfigError("pr-step: 'repo: owner/name' fehlt")
    token = _gh_token()
    base_branch = _substitute(step.get("base", "main"), ctx)
    head_branch = (
        _substitute(step["head"], ctx)
        if step.get("head")
        else _git(ctx, Path(ctx["workdir"]), "rev-parse", "--abbrev-ref", "HEAD")
    )
    validate_branch_name(base_branch)
    validate_branch_name(head_branch)
    # Duplikat-Check vor dem Create: offener PR mit gleichem Head-Branch →
    # wiederverwenden (Re-Runs/Resumes legen keinen zweiten PR an).
    labels = _normalize_labels(step.get("labels") or [])
    existing = _find_open_pr(repo_cfg, head_branch, token)
    if existing is not None:
        _export_pr(ctx, existing)
        _add_labels(repo_cfg, existing["number"], labels, token)
        return {
            "ok": True,
            "summary": f"PR #{existing['number']} existiert bereits (head {head_branch}) — wiederverwendet",
        }
    if _commits_ahead(repo_cfg, base_branch, head_branch, token) == 0:
        return {
            "ok": True,
            "done": True,
            "summary": f"keine Commits auf {head_branch} gegenüber {base_branch} — kein PR",
        }
    title = _substitute(step.get("title", "fabrik: {loop} {run_id}"), ctx)
    body = _substitute(step.get("body", "Erzeugt von **fabrik** (Loop `{loop}`, Trigger `{trigger}`)."), ctx)
    payload: dict[str, Any] = {"title": title, "head": head_branch, "base": base_branch, "body": body}
    res = _gh_api("POST", f"https://api.github.com/repos/{repo_cfg}/pulls", token, payload)
    if res.get("number") is not None:
        _export_pr(ctx, res)
        _add_labels(repo_cfg, res["number"], labels, token)
    return {"ok": True, "summary": f"PR #{res.get('number')} {res.get('html_url')}"}


def _export_pr(ctx: dict, pr: dict) -> None:
    """PR-Daten für Folge-Steps (pr_review, pr_merge, human_gate) in den ctx:
    ``pr_number``, ``pr_url`` und ``pr_title`` (Approval-Kontext, ADR 0011)."""
    ctx["pr_number"] = str(pr["number"])
    if pr.get("html_url"):
        ctx["pr_url"] = pr["html_url"]
    if pr.get("title"):
        ctx["pr_title"] = pr["title"]


def _add_labels(repo_cfg: str, number: Any, labels: list[str], token: str) -> None:
    """Labels an PR #number hängen (Issues-API; PRs sind Issues). Leer → kein Call."""
    if labels:
        _gh_api("POST", f"https://api.github.com/repos/{repo_cfg}/issues/{number}/labels", token, {"labels": labels})


def _find_open_pr(repo_cfg: str, head_branch: str, token: str) -> dict | None:
    """Ersten offenen PR mit Head-Branch `<owner>:<head_branch>` suchen.

    Deterministischer Duplikat-Check vor dem Create (GitHub erlaubt genau
    einen offenen PR pro Head-Branch). None, wenn keiner existiert.
    """
    owner = repo_cfg.split("/", 1)[0]
    res = _gh_api(
        "GET",
        f"https://api.github.com/repos/{repo_cfg}/pulls?head={quote(f'{owner}:{head_branch}')}&state=open",
        token,
    )
    if isinstance(res, list) and res:
        return res[0]
    return None


def _commits_ahead(repo_cfg: str, base_branch: str, head_branch: str, token: str) -> int | None:
    """Anzahl Commits von `head_branch` vor `base_branch` (Compare-API), None wenn unbekannt.

    Branch-Namen werden vollständig percent-encoded (`safe=""`): ein `/` im
    Namen (z. B. `release/1.0`) wäre im Compare-Pfad sonst ein zweites
    Pfad-Segment → 404. HTTP-Fehler (z. B. 404, weil der Head-Branch auf dem
    Remote nicht existiert) → None: der Create-Pfad liefert dann die klarere
    GitHub-Fehlermeldung."""
    try:
        res = _gh_api(
            "GET",
            f"https://api.github.com/repos/{repo_cfg}/compare/{quote(base_branch, safe='')}...{quote(head_branch, safe='')}",
            token,
        )
    except urllib.error.HTTPError:
        return None
    ahead = res.get("ahead_by") if isinstance(res, dict) else None
    return ahead if type(ahead) is int else None


def _normalize_labels(labels: Iterable[Any]) -> list[str]:
    """labels-Config → API-Liste: als Strings, leer gefiltert, dedupliziert.

    Reihenfolge bleibt erhalten; nicht-String-Werte (z. B. Zahlen, None,
    Dicts aus YAML) werden per `str()` coerced. Leere Liste → kein
    Labels-Call.
    """
    seen: set[str] = set()
    out: list[str] = []
    for x in labels:
        s = str(x).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out
