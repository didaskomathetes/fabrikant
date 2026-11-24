#!/usr/bin/env python3
"""check-adr-numbers — ADR number collisions between parallel branches.

Agents pick the ADR number relative to their own branch state; parallel branches
therefore inevitably collide (ADR 0032).
This script checks deterministically against a base (default `origin/main`) —
not via prompt logic:

  check [--base B] [--head H]
      New ADRs (added in the branch) whose number is already taken on B
      (or that duplicate each other) → list on stdout, exit 1.
      Without --head: working state (HEAD + uncommitted + untracked), otherwise `B...H`.
  next [--base B] [--repo R] [--no-prs]
      Next free number according to B AND all open PRs of R (highest taken + 1;
      gaps are not filled). The PR of the current branch does not count.
  fix [--base B] [--repo R] [--no-prs]
      Rename colliding NEW ADRs to the next free numbers (also in
      open PRs too): file name, title line `# ADR NNNN`, file path references as well as
      `ADR NNNN` in lines added by the branch. ADRs that exist on B are never
      touched.

`check` only compares with B (deterministic, CI-friendly). Open PRs are read via `gh`;
without `gh`/network only B counts, with a warning.

Runs in the package directory (`fabrikant/`); paths are relative to the cwd.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ADR_DIR = "docs/adr"
DEFAULT_BASE = "origin/main"
DEFAULT_REPO = "didaskomathetes/fabrikant"
PR_LIMIT = 200  # open PRs whose files are read
# Laufzeitdaten/Umgebungen: `fix` schreibt dort nie (Run-Transkripte bleiben Historie).
SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", "runs", "state"})
ADR_FILE_RE = re.compile(r"^(\d{4})-.+\.md$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
NUMBER_WIDTH = 4


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def adr_number(path: str) -> int | None:
    """ADR number from the file name (`0013-foo.md` → 13), None for non-ADRs (README)."""
    m = ADR_FILE_RE.match(Path(path).name)
    return int(m.group(1)) if m else None


def _new_adr_number(path: str) -> int:
    """Number of a path from `new_adrs` (filtered to ADRs there, so never None)."""
    n = adr_number(path)
    if n is None:
        raise ValueError(f"not an ADR file name: {path}")
    return n


def base_numbers(base: str) -> set[int]:
    """Auf `base` vergebene ADR-Nummern."""
    out = _git("ls-tree", "-r", "--name-only", base, "--", f"{ADR_DIR}/")
    return {n for line in out.splitlines() if (n := adr_number(line)) is not None}


def _merge_base(base: str) -> str:
    return _git("merge-base", base, "HEAD").strip()


def _working_diffs(base: str, *args: str) -> list[str]:
    """Diffs from the base covering both the index and working tree."""
    merge_base = _merge_base(base)
    return [
        _git("diff", *args, "--relative", merge_base, "--"),
        _git("diff", "--cached", *args, "--relative", merge_base, "--"),
    ]


def new_adrs(base: str, head: str | None = None) -> list[str]:
    """ADR files newly added in the branch (sorted by number, then name)."""
    if head is None:
        diffs = _working_diffs(base, "--name-status", "--no-renames", "--diff-filter=A")
        untracked = _git("ls-files", "--others", "--exclude-standard", "--", f"{ADR_DIR}/")
        paths = [line.split("\t", 1)[1] for diff in diffs for line in diff.splitlines()] + untracked.splitlines()
    else:
        diff = _git("diff", "--name-status", "--no-renames", "--diff-filter=A", "--relative", f"{base}...{head}", "--")
        paths = [line.split("\t", 1)[1] for line in diff.splitlines()]
    adrs = {p for p in paths if p.startswith(f"{ADR_DIR}/") and adr_number(p) is not None}
    return sorted(adrs, key=lambda p: (adr_number(p), p))


def collisions(base: str, head: str | None = None) -> list[str]:
    """New ADRs whose number is already taken (on base or by another new ADR)."""
    taken = base_numbers(base)
    seen: set[int] = set()
    clashing = []
    for path in new_adrs(base, head):
        n = _new_adr_number(path)
        if n in taken or n in seen:
            clashing.append(path)
        seen.add(n)
    return clashing


def _gh_open_prs(repo: str) -> list[dict]:
    """Offene PRs (`headRefName`, `files`) via gh."""
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        str(PR_LIMIT),
        "--json",
        "headRefName,files",
    ]
    return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)


def pr_numbers(repo: str) -> set[int]:
    """ADR numbers taken in open PRs, excluding the PR of the current branch (otherwise
    `fix` would keep moving its own ADR). PR paths are repo-relative.

    Without `gh`/network: warning on stderr, empty set (then only the base counts)."""
    try:
        prs = _gh_open_prs(repo)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as e:
        print(f"Warnung: offene PRs nicht lesbar ({e}) — nur die Base zählt", file=sys.stderr)
        return set()
    adr_dir = _git("rev-parse", "--show-prefix").strip() + ADR_DIR
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
    return {
        n
        for pr in prs
        if pr.get("headRefName") != branch
        for f in pr.get("files") or []
        if Path(f["path"]).parent.as_posix() == adr_dir and (n := adr_number(f["path"])) is not None
    }


def next_free(taken: set[int]) -> int:
    """Highest taken number + 1 (gaps stay gaps)."""
    return max(taken, default=0) + 1


def _stem(path: str) -> str:
    return Path(path).name.removesuffix(".md")


def renumbered_name(path: str, number: int) -> str:
    """File name with a new number (`0013-foo.md`, 14 → `0014-foo.md`)."""
    return f"{number:0{NUMBER_WIDTH}d}{Path(path).name[NUMBER_WIDTH:]}"


def added_lines(base: str) -> dict[str, set[int] | None]:
    """Files changed by the branch (incl. working state) → added line numbers.

    None = whole file is new (all lines).

    Includes files changed relative to the merge base (committed, staged or
    in the working state) as well as whole untracked/ignored files (except SKIP_DIRS);
    unchanged tracked files are left alone. ``fix`` additionally excludes existing
    ADRs of the base.
    """
    out: dict[str, set[int] | None] = {}
    tracked = set(_git("ls-files", "--cached").splitlines())
    for path in _working_files():
        if path not in tracked:
            out[path] = None
    for diff in _working_diffs(base, "-U0", "--no-renames"):
        path = ""
        for line in diff.splitlines():
            if line.startswith("+++ "):
                path = line[len("+++ b/") :] if line.startswith("+++ b/") else ""
            elif path and (m := HUNK_RE.match(line)):
                start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                lines = out.setdefault(path, set())
                if lines is not None:
                    lines.update(range(start, start + count))
    return out


def _working_files() -> set[str]:
    """Return readable files in the working tree, including ignored files (except SKIP_DIRS)."""
    return {
        path.as_posix() for path in Path(".").rglob("*") if path.is_file() and not SKIP_DIRS.intersection(path.parts)
    }


def rewrite_text(text: str, renames: dict[str, tuple[int, int]], lines: set[int] | None, is_adr: bool) -> str:
    """Update references to renamed ADRs.

    File names/stems everywhere; `ADR NNNN` (old number) only in added lines
    (`lines`; None = all) — the rest of the file belongs to the state on base. In the
    renamed ADR itself (`is_adr`) this applies to all lines."""
    out = []
    for i, line in enumerate(text.splitlines(keepends=True), start=1):
        for old_stem, (old, new) in renames.items():
            new_stem = renumbered_name(old_stem, new)
            line = line.replace(old_stem, new_stem).replace(f"[{old:04d}]({new_stem}", f"[{new:04d}]({new_stem}")
            if is_adr or lines is None or i in lines:
                line = re.sub(rf"\bADR {old:04d}\b", f"ADR {new:04d}", line)
        out.append(line)
    return "".join(out)


def _move(old_path: str, new_path: str) -> None:
    """Rename a file; for tracked/staged files via `git mv` so the index stays consistent."""
    if _git("ls-files", "--cached", "--", old_path).strip():
        _git("mv", old_path, new_path)
    else:
        Path(old_path).rename(new_path)


def fix(base: str, reserved: frozenset[int] = frozenset()) -> list[tuple[str, str]]:
    """Kollidierende neue ADRs umbenennen; liefert [(alter Pfad, neuer Pfad)].

    `reserved`: additionally taken numbers (open PRs) that new numbers skip."""
    clashing = collisions(base)
    if not clashing:
        return []
    taken = base_numbers(base) | reserved | {_new_adr_number(p) for p in new_adrs(base) if p not in clashing}
    renames: dict[str, tuple[int, int]] = {}  # alter Stem → (alte, neue Nummer)
    moves: list[tuple[str, str]] = []
    for path in clashing:
        number = next_free(taken)
        taken.add(number)
        new_path = str(Path(path).with_name(renumbered_name(path, number)))
        renames[_stem(path)] = (_new_adr_number(path), number)
        moves.append((path, new_path))
    touched = added_lines(base)
    for old_path, new_path in moves:
        _move(old_path, new_path)
        if old_path in touched:
            touched[new_path] = touched.pop(old_path)
    moved = {new for _, new in moves}
    for path, lines in touched.items():
        p = Path(path)
        if not p.is_file() or (path.startswith(f"{ADR_DIR}/") and path not in moved and adr_number(path) is not None):
            continue  # existing ADRs (base) stay untouched
        try:
            text = p.read_text()
        except UnicodeDecodeError:
            continue
        new_text = rewrite_text(text, renames, lines, is_adr=path in moved)
        if new_text != text:
            p.write_text(new_text)
    return moves


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("check", "next", "fix"):
        sp = sub.add_parser(name)
        sp.add_argument("--base", default=DEFAULT_BASE)
        if name != "check":
            sp.add_argument("--repo", default=DEFAULT_REPO)
            sp.add_argument("--no-prs", action="store_true", help="ignore open PRs")
    sub.choices["check"].add_argument("--head", default=None)
    args = p.parse_args(argv)

    reserved = frozenset() if args.cmd == "check" or args.no_prs else frozenset(pr_numbers(args.repo))
    if args.cmd == "next":
        print(f"{next_free(base_numbers(args.base) | reserved):0{NUMBER_WIDTH}d}")
    elif args.cmd == "fix":
        for old, new in fix(args.base, reserved):
            print(f"{old} -> {new}")
    else:
        clashing = collisions(args.base, args.head)
        for path in clashing:
            print(f"ADR number {adr_number(path):04d} collides with {args.base}: {path}")
        return 1 if clashing else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
