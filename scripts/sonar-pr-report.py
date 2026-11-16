#!/usr/bin/env python3
"""Deterministischer Quality-Report eines PRs aus SonarQube + diff-cover (advisory).

Aufruf durch `scripts/sonar-scan.sh --pr <n>` nach erfolgreicher Analyse unter dem
PR-eigenen Project-Key (Community Edition: keine native PR-Analyse):

    sonar-pr-report.py --pr 12 --project-key quark-playground:fabrik:pr-12 \
        [--base main] [--coverage coverage.xml] [--repo owner/name] [--post]

- Issues (`api/issues/search`) nur auf den im PR geänderten Zeilen
  (`git diff -U0 origin/<base>...HEAD`), stabil sortiert (Severity, Pfad, Zeile, Regel).
- Lint (ruff check + ruff format + mypy) nur auf den geänderten Zeilen.
- Kennzahlen des PR-Keys (`api/measures/component`).
- Coverage der geänderten Zeilen über `diff-cover` (JSON-Report).

Ausgabe: Markdown nach stdout, letzte Zeile `<!-- fabrik-quality sha=<head> -->`.
`--post` legt den Report als genau einen PR-Kommentar an bzw. aktualisiert den
bestehenden mit Marker (gh-CLI, Auth wie `scripts/pr-status.py`).
Env: `SONAR_HOST_URL` (Default http://127.0.0.1:9000), `SONAR_TOKEN`.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

MARKER_PREFIX = "<!-- fabrik-quality"
MARKER_RE = re.compile(r"<!-- fabrik-quality sha=([0-9a-f]{7,40}) -->")
DEFAULT_HOST = "http://127.0.0.1:9000"
DEFAULT_BASE = "main"
METRICS = ("bugs", "vulnerabilities", "code_smells", "duplicated_lines_density", "coverage")
SEVERITY_ORDER = ("BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO")
ISSUE_PAGE_SIZE = 500
MAX_ISSUE_PAGES = 20  # Obergrenze: 10 k Issues reichen für einen PR-Report
COMMENT_PAGE_SIZE = 100
MAX_COMMENT_PAGES = 10  # Obergrenze: 1000 Kommentare reichen für einen PR
HTTP_TIMEOUT_S = 30
MAX_LISTED_ISSUES = 50

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


# ---------------------------------------------------------------- Diff
def parse_changed_lines(diff: str) -> dict[str, set[int]]:
    """`git diff -U0` → {repo-relativer Pfad: geänderte/neue Zeilen im neuen Stand}."""
    changed: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = target[2:] if target.startswith("b/") else None  # /dev/null = gelöscht
            continue
        m = _HUNK_RE.match(line)
        if m and current is not None:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            if count:  # reiner Lösch-Hunk (+c,0) hat keine neuen Zeilen
                changed.setdefault(current, set()).update(range(start, start + count))
    return changed


# ---------------------------------------------------------------- Modell
@dataclass(frozen=True)
class Issue:
    """Sonar-Issue auf einer geänderten Zeile (Pfad relativ zur Repo-Wurzel)."""

    path: str
    line: int
    severity: str
    type: str
    rule: str
    message: str

    def sort_key(self) -> tuple:
        rank = SEVERITY_ORDER.index(self.severity) if self.severity in SEVERITY_ORDER else len(SEVERITY_ORDER)
        return (rank, self.path, self.line, self.rule)


@dataclass
class QualityReport:
    """Alles, was in den PR-Kommentar geht; `render()` ist deterministisch."""

    pr: int
    sha: str
    measures: dict[str, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    lint_issues: list[Issue] = field(default_factory=list)
    diff_coverage: float | None = None  # Prozent der geänderten, messbaren Zeilen
    uncovered: dict[str, list[int]] = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"## Quality-Report PR #{self.pr} (`{self.sha[:7]}`, SonarQube + diff-cover, advisory)", ""]
        cov = "–" if self.diff_coverage is None else f"{self.diff_coverage:.0f} %"
        lines.append(f"- Coverage geänderter Zeilen: **{cov}**")
        for path, nums in sorted(self.uncovered.items()):
            lines.append(f"  - ungedeckt: `{path}` Zeilen {', '.join(map(str, nums))}")
        measured = ", ".join(f"{m}={self.measures[m]}" for m in METRICS if m in self.measures)
        lines.append(f"- Kennzahlen (gesamter Stand): {measured or '–'}")
        for title, found in (("Sonar-Issues", self.issues), ("Lint (ruff + mypy)", self.lint_issues)):
            lines.append(f"- {title} auf geänderten Zeilen: **{len(found)}**")
            for issue in found[:MAX_LISTED_ISSUES]:
                lines.append(
                    f"  - [{issue.severity}/{issue.type}] `{issue.path}:{issue.line}` {issue.message} ({issue.rule})"
                )
            if len(found) > MAX_LISTED_ISSUES:
                lines.append(f"  - … {len(found) - MAX_LISTED_ISSUES} weitere")
        lines += ["", f"{MARKER_PREFIX} sha={self.sha} -->"]
        return "\n".join(lines)


# ---------------------------------------------------------------- Sonar
def sonar_get(host: str, token: str, path: str, params: dict) -> dict:
    """GET gegen die Sonar-Web-API (Basic-Auth `<token>:`)."""
    url = f"{host.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    auth = base64.b64encode(f"{token}:".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read())


def fetch_issues(host: str, token: str, key: str) -> list[dict]:
    """Alle offenen Issues des Project-Keys (paginiert, max. `MAX_ISSUE_PAGES` Seiten)."""
    issues: list[dict] = []
    page = 1
    while page <= MAX_ISSUE_PAGES:
        data = sonar_get(
            host,
            token,
            "/api/issues/search",
            {"componentKeys": key, "resolved": "false", "ps": ISSUE_PAGE_SIZE, "p": page},
        )
        batch = data.get("issues") or []
        issues.extend(batch)
        if len(batch) < ISSUE_PAGE_SIZE:
            return issues
        page += 1
    return issues


def fetch_measures(host: str, token: str, key: str) -> dict[str, str]:
    data = sonar_get(host, token, "/api/measures/component", {"component": key, "metricKeys": ",".join(METRICS)})
    return {m["metric"]: str(m.get("value", "")) for m in (data.get("component") or {}).get("measures") or []}


def issues_on_changed_lines(raw: list[dict], key: str, prefix: str, changed: dict[str, set[int]]) -> list[Issue]:
    """Sonar-Issues → nur die auf geänderten Zeilen, stabil sortiert.

    `component` ist `<key>:<pfad relativ zum Scan-Verzeichnis>`; `prefix` macht daraus
    den repo-relativen Pfad des Diffs (z. B. `fabrik/`)."""
    result = []
    for item in raw:
        component, line = str(item.get("component", "")), item.get("line")
        if not component.startswith(f"{key}:") or not isinstance(line, int):
            continue
        path = prefix + component[len(key) + 1 :]
        if line in changed.get(path, set()):
            result.append(
                Issue(
                    path=path,
                    line=line,
                    severity=str(item.get("severity", "")),
                    type=str(item.get("type", "")),
                    rule=str(item.get("rule", "")),
                    message=str(item.get("message", "")).replace("\n", " "),
                )
            )
    return sorted(result, key=Issue.sort_key)


# ---------------------------------------------------------------- Lint
def _run(cmd: list[str]) -> str:
    """Subprocess (advisory): stdout egal ob Exit 0 oder nicht, '' wenn das Tool fehlt."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.stdout


def _json_values(text: str) -> list[dict]:
    """JSON-Array ODER JSON-Lines (mypy) → Liste von Objekten; [] bei leerem/ungültigem Output."""
    text = text.strip()
    if not text:
        return []
    candidates: list = []
    try:
        data = json.loads(text)
        candidates = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        try:
            candidates = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return []
    return [d for d in candidates if isinstance(d, dict)]


def lint_issues(changed: dict[str, set[int]], prefix: str) -> list[Issue]:
    """ruff (check + format) und mypy — nur Befunde auf geänderten Zeilen, stabil sortiert.

    Pfade der Tools normalisieren wir relativ zum Aufruf-Verzeichnis (FABRIK_DIR;
    ruff meldet absolute Pfade); `prefix` macht daraus den repo-relativen Pfad des
    Diffs (z. B. `fabrik/`). Ein defektes Tool (fehlend, ungültiger Output) liefert
    einfach keine Befunde — advisory."""
    cwd = Path.cwd().resolve()

    def rel(path: str) -> str:
        try:
            return Path(path).resolve().relative_to(cwd).as_posix()
        except (ValueError, OSError):
            return ""  # außerhalb des Aufruf-Verzeichnisses → nicht zuordenbar

    def row(diag: dict) -> int:
        loc = diag.get("location")
        value = loc.get("row") if isinstance(loc, dict) else diag.get("line", 0)
        if value is None:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    raw: list[tuple[str, str, int, str, str]] = []  # (pfad, severity, zeile, regel, message)
    for diag in _json_values(_run(["ruff", "check", "--output-format", "json"])):
        raw.append(
            (
                rel(str(diag.get("filename", ""))),
                str(diag.get("severity", "error")),
                row(diag),
                f"ruff:{diag.get('code', '')}",
                str(diag.get("message", "")).replace("\n", " "),
            )
        )
    for diag in _json_values(_run(["ruff", "format", "--check", "--output-format", "json"])):
        raw.append(
            (
                rel(str(diag.get("filename", ""))),
                "error",
                row(diag),
                "ruff-format",
                str(diag.get("message", "")).replace("\n", " "),
            )
        )
    for diag in _json_values(_run(["mypy", "--output", "json", "src"])):
        raw.append(
            (
                rel(str(diag.get("file", ""))),
                str(diag.get("severity", "error")),
                row(diag),
                f"mypy:{diag.get('code', '')}",
                str(diag.get("message", "")).replace("\n", " "),
            )
        )
    result = []
    for path, severity, line, rule, message in raw:
        if not path:
            continue
        repo_path = prefix + path
        if line in changed.get(repo_path, set()):
            result.append(Issue(path=repo_path, line=line, severity=severity, type="LINT", rule=rule, message=message))
    return sorted(result, key=Issue.sort_key)


# ---------------------------------------------------------------- diff-cover
def diff_coverage(coverage_xml: Path, compare_ref: str) -> tuple[float | None, dict[str, list[int]]]:
    """(Prozent, {pfad: ungedeckte Zeilen}) der geänderten Zeilen; (None, {}) ohne messbare Zeilen.

    `compare_ref` ist der Merge-Base-Commit: diff-cover diffet Workdir gegen den
    Ref (zweipunktig) — mit dem Merge-Base deckt sich das mit `origin/<base>...HEAD`."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "diff-cover.json"
        subprocess.run(
            ["diff-cover", str(coverage_xml), f"--compare-branch={compare_ref}", "--format", f"json:{out}"],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(out.read_text(encoding="utf-8"))
    uncovered = {
        p: sorted(s["violation_lines"]) for p, s in (data.get("src_stats") or {}).items() if s.get("violation_lines")
    }
    if not data.get("total_num_lines"):
        return None, uncovered
    return float(data.get("total_percent_covered", 0)), uncovered


# ---------------------------------------------------------------- GitHub
def _gh(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, input=stdin).stdout


def find_marker_comment(comments: list[dict]) -> int | None:
    """ID des (ersten) Kommentars mit Quality-Marker, sonst None."""
    for c in comments:
        if MARKER_PREFIX in str(c.get("body", "")):
            return int(c["id"])
    return None


def list_comments(repo: str, pr: int) -> list[dict]:
    """Alle PR-Kommentare (paginiert, max. `MAX_COMMENT_PAGES` Seiten)."""
    comments: list[dict] = []
    page = 1
    while page <= MAX_COMMENT_PAGES:
        batch = json.loads(_gh("api", f"repos/{repo}/issues/{pr}/comments?per_page={COMMENT_PAGE_SIZE}&page={page}"))
        comments.extend(batch)
        if len(batch) < COMMENT_PAGE_SIZE:
            return comments
        page += 1
    return comments


def upsert_comment(repo: str, pr: int, body: str) -> str:
    """Genau ein Quality-Kommentar pro PR: bestehenden aktualisieren, sonst anlegen. → 'updated'|'created'."""
    payload = json.dumps({"body": body})
    existing = find_marker_comment(list_comments(repo, pr))
    if existing is not None:
        _gh("api", "-X", "PATCH", f"repos/{repo}/issues/comments/{existing}", "--input", "-", stdin=payload)
        return "updated"
    _gh("api", "-X", "POST", f"repos/{repo}/issues/{pr}/comments", "--input", "-", stdin=payload)
    return "created"


# ---------------------------------------------------------------- CLI
def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def build_report(args: argparse.Namespace, host: str, token: str) -> QualityReport:
    compare_ref = f"origin/{args.base}"
    changed = parse_changed_lines(_git("diff", "-U0", f"{compare_ref}...HEAD"))
    # diff-cover diffet Workdir gegen den Ref (zweipunktig); der Merge-Base macht
    # den Bereich identisch zu `origin/<base>...HEAD` (s. diff_coverage).
    merge_base = _git("merge-base", compare_ref, "HEAD").strip()
    prefix = _git("rev-parse", "--show-prefix").strip()
    report = QualityReport(pr=args.pr, sha=_git("rev-parse", "HEAD").strip())
    report.measures = fetch_measures(host, token, args.project_key)
    report.issues = issues_on_changed_lines(
        fetch_issues(host, token, args.project_key), args.project_key, prefix, changed
    )
    report.lint_issues = lint_issues(changed, prefix)
    if args.coverage.is_file():
        report.diff_coverage, report.uncovered = diff_coverage(args.coverage, merge_base)
    return report


def main(argv: list[str] | None = None) -> int:
    doc_lines = (__doc__ or "").splitlines()
    ap = argparse.ArgumentParser(description=doc_lines[0] if doc_lines else "")
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--project-key", required=True)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--coverage", type=Path, default=Path("coverage.xml"))
    ap.add_argument("--repo", default="", help="owner/name (Default: gh repo view)")
    ap.add_argument("--post", action="store_true", help="Report als PR-Kommentar anlegen/aktualisieren")
    args = ap.parse_args(argv)
    report = build_report(args, os.environ.get("SONAR_HOST_URL", DEFAULT_HOST), os.environ.get("SONAR_TOKEN", ""))
    body = report.render()
    print(body)
    if args.post:
        repo = args.repo or _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").strip()
        print(f"[sonar-pr-report] PR-Kommentar {upsert_comment(repo, args.pr, body)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
