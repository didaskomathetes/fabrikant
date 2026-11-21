"""Step: pr (PR anlegen) — über den Hosting-Provider (ADR 0062).

Idempotent (Duplikat-Schutz): existiert für den Head-Branch bereits ein
offener PR, wird er wiederverwendet — GitHub erlaubt ohnehin nur einen
offenen PR pro Head-Branch, der Create-Call würde also fehlschlagen.
Wichtig für Re-Runs/Resumes desselben Issues.

Leerer Head: hat `head` keine Commits vor `base` (z. B. Agent fand nichts zu
tun, `ship` war ein No-Op), lehnt der Host den Create mit 422 ab. Der Step
prüft das vorher über die Compare-API und beendet den Run dann erfolgreich
(`done`) statt mit Fehler — Folge-Steps (Review, Merge) hätten keinen PR.

Labels: `POST /pulls` kennt keinen labels-Parameter (GitHub ignoriert ihn
stillschweigend) — sie werden danach über den Issues-Endpunkt gesetzt, auch
für wiederverwendete PRs (additiv, idempotent; heilt PRs ohne Label).

Der Step spricht den Hosting-Provider (ADR 0062), nicht die GitHub-API direkt:
GitHub und GitLab laufen über denselben Pfad.

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

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..context import _substitute
from ..failures import ConfigError
from ..hosting import Provider, PullRequest, get_provider
from ..validation import validate_branch_name
from .git import _git


def _step_pr(ctx: dict, step: dict) -> dict:
    repo_cfg = step.get("repo")  # "owner/name"
    if not repo_cfg:
        raise ConfigError("pr-step: 'repo: owner/name' fehlt")
    provider = get_provider(repo_cfg)
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
    existing = _find_open_pr(provider, repo_cfg, head_branch)
    if existing is not None:
        _export_pr(ctx, existing)
        provider.add_labels(repo_cfg, existing.number, labels)
        return {
            "ok": True,
            "summary": f"PR #{existing.number} existiert bereits (head {head_branch}) — wiederverwendet",
        }
    if _commits_ahead(provider, repo_cfg, base_branch, head_branch) == 0:
        return {
            "ok": True,
            "done": True,
            "summary": f"keine Commits auf {head_branch} gegenüber {base_branch} — kein PR",
        }
    title = _substitute(step.get("title", "fabrik: {loop} {run_id}"), ctx)
    body = _substitute(step.get("body", "Erzeugt von **fabrik** (Loop `{loop}`, Trigger `{trigger}`)."), ctx)
    pr = provider.create_pr(repo_cfg, title=title, head=head_branch, base=base_branch, body=body)
    _export_pr(ctx, pr)
    provider.add_labels(repo_cfg, pr.number, labels)
    return {"ok": True, "summary": f"PR #{pr.number} {pr.url}"}


def _export_pr(ctx: dict, pr: PullRequest) -> None:
    """PR-Daten für Folge-Steps (pr_review, pr_merge, human_gate) in den ctx:
    ``pr_number``, ``pr_url`` und ``pr_title`` (Approval-Kontext, ADR 0011)."""
    ctx["pr_number"] = str(pr.number)
    if pr.url:
        ctx["pr_url"] = pr.url
    if pr.title:
        ctx["pr_title"] = pr.title


def _find_open_pr(provider: Provider, repo_cfg: str, head_branch: str) -> PullRequest | None:
    """Ersten offenen PR mit Head-Branch `<owner>:<head_branch>` suchen.

    Deterministischer Duplikat-Check vor dem Create (GitHub erlaubt genau
    einen offenen PR pro Head-Branch). None, wenn keiner existiert.
    """
    owner = repo_cfg.split("/", 1)[0]
    prs = provider.list_prs(repo_cfg, head=f"{owner}:{head_branch}", state="open")
    return prs[0] if prs else None


def _commits_ahead(provider: Provider, repo_cfg: str, base_branch: str, head_branch: str) -> int | None:
    """Anzahl Commits von `head_branch` vor `base_branch` (Compare-API), None wenn unbekannt.

    HTTP-Fehler (z. B. 404, weil der Head-Branch auf dem Remote nicht existiert)
    → None: der Create-Pfad liefert dann die klarere Host-Fehlermeldung."""
    try:
        return provider.compare(repo_cfg, base_branch, head_branch).ahead_by
    except Exception:  # noqa: BLE001 — Provider-Fehler → Create-Pfad entscheidet
        return None


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
