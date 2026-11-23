#!/usr/bin/env python3
"""pr-status — deterministische PR-Entscheidungen für review-pr/pr-reply (ADR 0024).

Die Loop-Agents sollen Reviews nicht selbst zusammenbauen und Merge-Freigabe
bzw. "neue Kommentare" nicht aus Roh-JSON ableiten (LLM-Raten = Self-Loops,
Doppel-Posts, Fehl-Merges). Dieses Script kapselt das über `gh`:

  post-review <num> --repo R --payload state/review-<num>.json [--comment-only]
      Ein GitHub-Review (Summary + Inline-Kommentare) posten — nur für den
      aktuellen Head-SHA, idempotent pro SHA, max. MAX_INLINE Inline-Kommentare.
      `--comment-only`: immer COMMENT statt APPROVE/REQUEST_CHANGES — für Repos, in
      denen das Verdikt beratend bleibt und Menschen approven (Team-Repos mit CODEOWNERS).
      Inline-Zeilen außerhalb der Diff-Hunks wandern vorab in die Summary;
      422 von GitHub → Fallback ganz ohne Inline.
  merge-check <num> --repo R
      → {"action": "merge" | "resolve_conflicts" | "wait", "reason": "..."}
      (mergeStateStatus-Zuordnung explizit: MERGE_STATES / REBASE_STATES / WAIT_STATES).
      Legt der PR eine ADR mit einer auf der Base schon vergebenen Nummer an → "wait"
      (ADR 0032; Reparatur: `scripts/check-adr-numbers.py fix`). Label
      'needs approval' → "wait": solche PRs mergt nur der approval-Loop (ADR 0011).
      Label `needs human` (`--human-label`) → "wait": eskaliert (ADR 0059).
  new-comments <num> --repo R --state state/replied-comments.json
      → [{"key", "kind", "id", "author", "created", "body", ...}] (chronologisch)
  escalation-check <num> --repo R [--stall-rounds N] [--max-rounds N] [--escalate] [--human-label L]
      Deterministische Eskalation (ADR 0059, kein LLM): `stalled` = die letzten N Verdikte des
      Reviewers sind `changes_requested`, ohne dass dazwischen ein Nicht-Merge-Commit kam;
      `exhausted` = `--max-rounds` Fix-Commits verbraucht und weiter `changes_requested` am Head.
      → {"action": "escalate" | "continue", "kind": "...", "reason": "..."}; `--escalate` setzt
      dann Label + Marker-Kommentar (siehe `escalate`).
  escalate <num> --repo R --kind K --why TEXT [--human-label L] [--builder]
      Eskalation an einen Menschen (ADR 0059): Kommentar mit Marker + Label L (Default
      `needs human`). Idempotent: trägt das Item das Label schon, passiert nichts.
      `--builder` (Builder-Entwurf, ADR 0059 §3): `--kind` nur aus BUILDER_ESCALATION_KINDS
      (`blocked`/`scope`) — nur der Host darf `stalled`/`exhausted`/`access` setzen.
  threads <num> --repo R
      → offene (unresolved) Review-Threads
  resolve-thread <num> --repo R --comment-id <id>
      Den Review-Thread mit diesem Inline-Kommentar als erledigt markieren.

Reviewer-Account: `FABRIK_REVIEWER_LOGIN` (Pflicht bei GitHub-App-Tokens — die dürfen
`/user` nicht abfragen), sonst `gh api user`. Bot-Logins werden normalisiert
(`app/<name>` aus gh-JSON ≡ `<name>[bot]` aus REST ≡ `<name>`).

Marker (HTML-Kommentare, im gerenderten Text unsichtbar):
  <!-- fabrik-review verdict=approved|changes_requested sha=<40 hex> -->
      Verdikt im Review-Body. Gilt nur vom Reviewer-Account (`gh api user`) und
      nur für den aktuellen Head-SHA — neue Commits brauchen ein neues Review.
      Nötig, weil GitHub Approve/Request-Changes auf eigene PRs verbietet.
  <!-- fabrik-reply -->
      Antwort des pr-reply-Loops. Solche Kommentare sind nie "neu" (kein Self-Loop).
  <!-- fabrik-escalate kind=<kind>[ re=<key>,…] -->
      Eskalation an einen Menschen (ADR 0059); nur der Host schreibt ihn (Agent-Text wird
      von allen fabrik-Markern bereinigt). Der escalation-Loop zeigt den letzten an.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REVIEW_MARKER_RE = re.compile(r"<!-- fabrik-review verdict=(approved|changes_requested) sha=([0-9a-f]{40}) -->")
REPLY_MARKER = "<!-- fabrik-reply -->"
VERDICT_APPROVED = "approved"
PAGE_SIZE = 100
MAX_INLINE = 10  # mehr Inline-Kommentare = Rauschen; Rest landet in der Summary
THREADS_LIMIT = 100  # GraphQL-Seite; mehr offene Threads pro PR sind kein realistischer Fall
HTTP_UNPROCESSABLE = "HTTP 422"
# Review-States, die eine Entscheidung tragen (COMMENTED/DISMISSED/PENDING nicht).
DECISIVE_REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED")
REVIEW_EVENTS = {VERDICT_APPROVED: "APPROVE", "changes_requested": "REQUEST_CHANGES"}
COMMENT_EVENT = "COMMENT"  # eigene PRs (GitHub erlaubt nur Kommentar-Reviews) oder --comment-only
OVERFLOW_HEADING = "### Weitere Anmerkungen (nicht inline)"
HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# mergeStateStatus (GitHub GraphQL) → Aktion. Jeder bekannte Status ist explizit
# zugeordnet; unbekannte (neue GitHub-Werte) → wait mit eigenem Grund.
ACTION_MERGE, ACTION_REBASE, ACTION_WAIT = "merge", "resolve_conflicts", "wait"
MERGE_STATES = ("CLEAN", "HAS_HOOKS")  # mergebar, Checks grün (HAS_HOOKS: nur Pre-Receive-Hooks)
REBASE_STATES = {
    "DIRTY": "Merge-Konflikt",
    "BEHIND": "Branch hinter Base (Rebase nötig)",
}
# ADR-PRs mit diesem Label mergt nur der approval-Loop nach menschlicher Freigabe
# (ADR 0011) — pr-reply darf sie trotz Review-Approve nicht mergen.
APPROVAL_LABEL = "needs approval"
# Eskalation an einen Menschen (ADR 0059): Label stoppt review-pr/pr-reply/develop und Merge.
HUMAN_LABEL = "needs human"
ESCALATION_KINDS = ("blocked", "stalled", "exhausted", "scope", "access")
BUILDER_ESCALATION_KINDS = ("blocked", "scope")  # nur diese darf der Builder per Entwurf setzen
ESCALATE_MARKER_RE = re.compile(r"<!-- fabrik-escalate kind=(\w+)(?: re=([\w,-]+))? -->")
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
    """`gh <args>` ausführen und stdout zurückgeben (Fehler → CalledProcessError)."""
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, input=stdin).stdout


def _gh_json(*args: str, stdin: str | None = None):
    """Wie _gh_text, stdout als JSON geparst."""
    return json.loads(_gh_text(*args, stdin=stdin))


def fetch_paged(path: str) -> list[dict]:
    """Alle Einträge einer paginierten REST-Liste (`?per_page=&page=`)."""
    items: list[dict] = []
    page = 1
    while True:
        batch = _gh_json("api", f"{path}?per_page={PAGE_SIZE}&page={page}")
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            return items
        page += 1


REVIEWER_LOGIN_ENV = "FABRIK_REVIEWER_LOGIN"
BOT_PREFIX, BOT_SUFFIX = "app/", "[bot]"


def normalize_login(login: str) -> str:
    """Bot-Logins vergleichbar machen: `app/x` (gh-JSON) und `x[bot]` (REST) → `x`."""
    return login.removeprefix(BOT_PREFIX).removesuffix(BOT_SUFFIX)


def reviewer_login() -> str:
    override = os.environ.get(REVIEWER_LOGIN_ENV, "").strip()
    return normalize_login(override or _gh_json("api", "user")["login"])


def _login(obj: dict) -> str:
    """Autor-Login (normalisiert) aus REST (`user`) oder GraphQL/gh-JSON (`author`)."""
    return normalize_login((obj.get("user") or obj.get("author") or {}).get("login", ""))


# ── ADR-Nummern-Kollision (ADR 0032) ──

ADR_DIR = "fabrik/docs/adr"  # repo-relativ, wie in `gh pr diff` und der Contents-API
ADR_FILE_RE = re.compile(r"^(\d{4})-.+\.md$")


def _adr_number(path: str) -> int | None:
    """Nummer einer ADR-Datei unter ADR_DIR (`…/0013-foo.md` → 13), sonst None."""
    match = ADR_FILE_RE.match(Path(path).name) if Path(path).parent.as_posix() == ADR_DIR else None
    return int(match.group(1)) if match else None


def _added_adr_numbers(files: list[dict]) -> list[int]:
    """Nummern neu angelegter ADRs aus `GET pulls/{n}/files`. Änderungen und Umbenennungen
    mit gleicher Nummer zählen nicht; eine Umbenennung auf eine andere Nummer zählt als neu."""
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
    """Kollisionsbeschreibung, wenn PR `num` eine ADR mit auf der Base vergebener (oder im
    PR doppelter) Nummer anlegt — fängt auch PR-gegen-PR-Kollisionen: Ist der erste PR
    gemergt, liegt seine Nummer auf der Base. Ohne `baseRefName` → None.

    Dateiliste per paginierter REST-API (kein `gh pr diff`: GitHub lehnt Diffs sehr
    großer PRs ab). Schlägt die Prüfung fehl → Beschreibung des Fehlers (fail closed:
    merge-check wartet, statt mit Traceback abzubrechen)."""
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


# ── Verdikt / Merge ──


def review_verdict(reviews: list[dict], reviewer: str) -> tuple[str, str] | None:
    """Letztes (verdict, sha) aus Review-Markern des Reviewer-Accounts, sonst None.

    Marker anderer Autoren werden ignoriert — sonst könnte jeder Kommentator
    eine Freigabe vortäuschen."""
    verdict = None
    for r in reviews:
        m = REVIEW_MARKER_RE.search(r.get("body") or "")
        if m and _login(r) == reviewer:
            verdict = (m.group(1), m.group(2))
    return verdict


def latest_plain_review_state(reviews: list[dict]) -> str:
    """Entscheidender State der Reviews OHNE fabrik-Marker ('' wenn keine).

    Das sind Menschen oder der pr_review-Step (ADR 0010) — sie gelten zusätzlich.
    Pro Autor zählt nur dessen letzte entscheidende Review (wie GitHubs
    reviewDecision): ein früheres CHANGES_REQUESTED ist überholt, sobald derselbe
    Autor danach — mit oder ohne Marker — erneut entscheidend reviewt. Sonst
    blockiert ein altes pr_review-Verdikt die PR für immer, weil alle späteren
    Reviews desselben Accounts Marker tragen. CHANGES_REQUESTED eines Autors
    schlägt APPROVED eines anderen."""
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
    """Trägt PR/Issue (gh-JSON oder REST, `labels: [{name}]`) das Label?"""
    return label in {str(lbl.get("name", "")) for lbl in item.get("labels") or []}


def merge_decision(pr: dict, reviewer: str, open_threads: int, human_label: str = HUMAN_LABEL) -> dict:
    """Merge-Entscheidung aus `gh pr view --json mergeStateStatus,headRefOid,reviews,labels`.

    Freigabe = Review-Marker 'approved' für den aktuellen Head-SHA, sonst eine
    GitHub-Review APPROVED. Blockiert durch CHANGES_REQUESTED ohne Marker
    (Mensch/pr_review-Step) und durch offene Review-Threads. Konflikt/Rückstand
    (REBASE_STATES) → resolve_conflicts unabhängig von der Freigabe. Label
    APPROVAL_LABEL → immer wait (Merge nur über den approval-Loop, ADR 0011); `human_label`
    → wait (eskaliert, ADR 0059)."""
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


# ── Review posten ──


def _overflow_section(comments: list[dict]) -> str:
    notes = [f"**`{c.get('path')}:{c.get('line')}`**\n{c.get('body', '')}" for c in comments]
    return OVERFLOW_HEADING + "\n\n" + "\n\n".join(notes)


def _insert_before_marker(body: str, section: str) -> str:
    """Abschnitt vor dem Verdikt-Marker einfügen (Marker bleibt letzte Zeile)."""
    m = REVIEW_MARKER_RE.search(body)
    if not m:
        return f"{body}\n\n{section}"
    return f"{body[: m.start()].rstrip()}\n\n{section}\n\n{body[m.start() :]}"


def diff_lines(diff: str) -> dict[str, list[set[int]]]:
    """Kommentierbare Zeilen (neuer Stand, RIGHT) aus einem Unified Diff: pro Datei
    eine Zeilenmenge je Hunk (`+`- und Kontextzeilen)."""
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
    """Liegen `line` (und ggf. `start_line`) im selben Hunk der Datei?"""
    wanted = {comment.get("line"), comment.get("start_line", comment.get("line"))}
    return any(wanted <= hunk for hunk in hunks.get(comment.get("path") or "", []))


def build_review(
    payload: dict, *, comment_only: bool, inline: bool = True, hunks: dict[str, list[set[int]]] | None = None
) -> dict:
    """GitHub-Review-Request aus dem Agent-Payload `{"body", "comments"}`.

    - Marker im Body ist Pflicht (Verdikt + reviewter SHA = `commit_id`).
    - Event: `comment_only` (eigene PR oder --comment-only) → COMMENT, sonst APPROVE/REQUEST_CHANGES.
    - Inline-Kommentare: nur Zeilen in `hunks` (falls gegeben, siehe diff_lines),
      max. MAX_INLINE; Rest (oder alle bei inline=False) als Liste in die Summary."""
    body = payload.get("body") or ""
    m = REVIEW_MARKER_RE.search(body)
    if not m:
        raise ValueError("Review-Body ohne fabrik-review-Marker")
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
    """Gibt es schon ein Review des Reviewers mit Marker für diesen SHA? (Retry-Schutz)"""
    return any(
        _login(r) == reviewer and (m := REVIEW_MARKER_RE.search(r.get("body") or "")) and m.group(2) == sha
        for r in reviews
    )


def post_review(repo: str, num: int, payload: dict, *, comment_only: bool = False) -> dict:
    """Review posten (siehe build_review); nur für den aktuellen Head-SHA,
    idempotent pro SHA, Inline außerhalb des Diffs → Summary, 422 → ohne Inline.
    `comment_only` erzwingt ein COMMENT-Review (eigene PRs bekommen es immer)."""
    reviewer = reviewer_login()
    pr = _gh_json("pr", "view", str(num), "--repo", repo, "--json", "author,reviews,headRefOid")
    comment_only = comment_only or _login(pr) == reviewer
    hunks = diff_lines(_gh_text("pr", "diff", str(num), "--repo", repo))
    request = build_review(payload, comment_only=comment_only, hunks=hunks)
    sha, head = request["commit_id"], pr.get("headRefOid", "")
    if sha != head:
        # Payload zu altem Commit: weder posten noch per Idempotenz "erledigt" melden.
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
    """Offene Threads kompakt: id, path, line, Autor + Text des ersten Kommentars."""
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
    """Thread, der den Inline-Kommentar (REST-ID = GraphQL databaseId) enthält."""
    for t in threads:
        if any(c.get("databaseId") == comment_id for c in t.get("comments", {}).get("nodes") or []):
            return t
    return None


# ── Neue Kommentare ──


def load_replied(path: Path) -> dict:
    """State `{"<key>": true}`; fehlend/kaputt → {}."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def collect_comments(num: int, issue: list[dict], reviews: list[dict], inline: list[dict]) -> list[dict]:
    """Alle drei Kommentar-Quellen normalisiert. State-Keys: `<num>-<id>` (PR-Kommentar,
    kompatibel zum alten State), `<num>-review-<id>`, `<num>-inline-<id>`."""
    items = [
        {"key": f"{num}-{c['id']}", "kind": "comment", "created": c.get("created_at", ""), "src": c} for c in issue
    ]
    items += [
        {"key": f"{num}-review-{r['id']}", "kind": "review", "created": r.get("submitted_at", ""), "src": r}
        for r in reviews
        if (r.get("body") or "").strip()  # Reviews nur mit Inline-Kommentaren haben keinen eigenen Text
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
    """Unbeantwortete fremde Kommentare: nicht im State, keine eigene Reply (Marker)."""
    return [i for i in items if i["key"] not in replied and REPLY_MARKER not in i["body"]]


# ── Eskalation (ADR 0059) ──

ESCALATE, CONTINUE = "escalate", "continue"
FIX_COMMIT_PREFIX = "fabrik: address review"  # Commit-Message der Fix-Commits (Rundenzähler)


def _verdicts(reviews: list[dict], reviewer: str) -> list[tuple[str, str]]:
    """Alle (verdict, sha) aus Marker-Reviews des Reviewer-Accounts, chronologisch."""
    out = []
    for r in reviews:
        m = REVIEW_MARKER_RE.search(r.get("body") or "")
        if m and _login(r) == reviewer:
            out.append((m.group(1), m.group(2)))
    return out


def _builder_changed(commits: list[dict], since: str, until: str) -> bool:
    """Gibt es zwischen `since` (exklusiv) und `until` (inklusiv) einen Nicht-Merge-Commit?
    `commits` = `GET pulls/{n}/commits` (chronologisch). Base-Syncs (`update-branch`) sind
    Merge-Commits und zählen nicht als Änderung durch den Builder. Unbekannte SHAs (Force-Push)
    → True, damit ein umgeschriebener Branch nie als Stillstand gilt."""
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
    """Deterministische Eskalation aus Review-Markern und PR-Commits (ADR 0059 §4).

    stalled:   die letzten `stall_rounds` Verdikte sind `changes_requested`, und der Builder hat
               dazwischen — und auch nach dem letzten Verdikt bis zum Head — keinen Code geändert
               (siehe _builder_changed).
    exhausted: `max_rounds` > 0 Fix-Commits (`FIX_COMMIT_PREFIX`) verbraucht und das Verdikt
               am aktuellen Head ist weiter `changes_requested`."""
    verdicts = _verdicts(reviews, reviewer)
    if not verdicts or verdicts[-1][0] == VERDICT_APPROVED:
        return {"action": CONTINUE, "reason": "kein offenes changes_requested"}
    rounds = max(2, stall_rounds)
    tail = verdicts[-rounds:]
    stuck = (
        len(tail) == rounds
        and all(v == "changes_requested" for v, _ in tail)
        and not any(_builder_changed(commits, a, b) for (_, a), (_, b) in zip(tail, tail[1:], strict=False))
        # Auch NACH dem letzten Verdikt: hat der Builder seitdem gepusht (Review ausstehend),
        # ist er aktiv — kein Stillstand (konsistent mit `exhausted` unten, das ebenfalls
        # den aktuellen Head prüft).
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
    """Eskalations-Marker (ADR 0059); `re_keys` = kommagetrennte Kommentar-Keys (optional)."""
    return f"<!-- fabrik-escalate kind={kind}{f' re={re_keys}' if re_keys else ''} -->"


def escalation_comment(kind: str, why: str) -> str:
    """Text des Eskalations-Kommentars (sichtbar + Marker für den escalation-Loop)."""
    return (
        f"**fabrik: an einen Menschen eskaliert** (`{kind}`)\n\n{why}\n\n"
        "Review, Reply und Merge pausieren, bis das Label entfernt ist. "
        "Entscheidung im fabrik-Gate: Fortsetzen, Neu zuschneiden oder Aufgeben.\n\n"
        f"{escalate_marker(kind)}"
    )


def escalate(
    repo: str, num: int, kind: str, why: str, human_label: str = HUMAN_LABEL, *, builder: bool = False
) -> dict:
    """Kommentar mit Marker posten und Label setzen; idempotent über das Label.

    `builder=True` (Builder-Entwurf, ADR 0059 §3) beschränkt `kind` auf
    BUILDER_ESCALATION_KINDS — nur der Host darf `stalled`/`exhausted`/`access` setzen.
    Der Agent erhält dadurch keine zusätzlichen Rechte."""
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
    """`escalation-check`: Entscheidung aus dem PR-Stand, mit `apply` gleich eskalieren."""
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
        cmds[name].add_argument("--human-label", default=HUMAN_LABEL, help="Eskalations-Label (ADR 0059)")
    cmds["escalation-check"].add_argument("--stall-rounds", type=int, default=DEFAULT_STALL_ROUNDS)
    cmds["escalation-check"].add_argument("--max-rounds", type=int, default=0, help="0 = keine exhausted-Prüfung")
    cmds["escalation-check"].add_argument("--escalate", action="store_true", help="bei Treffer Label + Kommentar")
    cmds["escalate"].add_argument("--kind", required=True, choices=ESCALATION_KINDS)
    cmds["escalate"].add_argument("--why", required=True)
    cmds["escalate"].add_argument(
        "--builder",
        action="store_true",
        help="Builder-Entwurf (ADR 0059 §3): --kind nur aus BUILDER_ESCALATION_KINDS",
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
