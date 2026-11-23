"""Metadata of a run for the detail modal: issue/PR with links (``GET /api/run/<id>``, field ``meta``).

Sources in this order: run variables (``issue_number``/``pr_url``/…), approval context,
trigger string (``on_issue:#N``/``on_pr:#N``) + the spec's ``github`` repo. Pure function, no I/O.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .engine import _issue_link, _pr_link


@dataclass(frozen=True)
class RunLink:
    """Verlinktes GitHub-Objekt eines Runs."""

    number: str = ""
    title: str = ""
    url: str = ""


def _clean(value: object) -> str:
    return str(value or "").strip()


def issue_link(spec: dict, record: dict) -> RunLink | None:
    """Triggering issue of the run (or ``None``)."""
    vars_ = record.get("vars") or {}
    trigger = _clean(record.get("trigger"))
    number = _clean(vars_.get("issue_number"))
    url = _issue_link(spec, trigger) or _clean((record.get("approval") or {}).get("issue"))
    if number and not url and spec.get("github"):
        url = f"https://github.com/{spec['github']}/issues/{number}"
    if not (number or url):
        return None
    return RunLink(number, _clean(vars_.get("issue_title")), url)


def pr_link(spec: dict, record: dict) -> RunLink | None:
    """PR of the run (or ``None``)."""
    vars_ = record.get("vars") or {}
    approval = record.get("approval") or {}
    url = _pr_link(spec, _clean(record.get("trigger")), vars_) or _clean(approval.get("pr"))
    number = _clean(vars_.get("pr_number")) or (url.rsplit("/", 1)[-1] if url else "")
    if not (number or url):
        return None
    return RunLink(number, _clean(vars_.get("pr_title") or approval.get("pr_title")), url)


def run_meta(spec: dict | None, record: dict) -> dict[str, dict]:
    """``{"issue": {...}, "pr": {...}}`` – only existing entries."""
    spec = spec or {}
    links = {"issue": issue_link(spec, record), "pr": pr_link(spec, record)}
    return {k: asdict(v) for k, v in links.items() if v}
