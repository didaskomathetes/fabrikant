#!/usr/bin/env python3
"""check-adr-numbers — ADR-Nummern-Kollisionen zwischen parallelen Branches.

Agents wählen die ADR-Nummer relativ zum eigenen Branch-Stand; parallele Branches
kollidieren dadurch zwangsläufig (ADR 0032).
Dieses Script prüft deterministisch gegen eine Base (Default `origin/main`) —
nicht per Prompt-Logik:

  check [--base B] [--head H]
      Neue ADRs (im Branch hinzugefügt), deren Nummer auf B schon vergeben ist
      (oder die untereinander doppelt sind) → Liste auf stdout, Exit 1.
      Ohne --head: Arbeitsstand (HEAD + uncommitted + untracked), sonst `B...H`.
  next [--base B] [--repo R] [--no-prs]
      Nächste freie Nummer laut B UND allen offenen PRs von R (höchste vergebene + 1;
      Lücken werden nicht gefüllt). Der PR des aktuellen Branches zählt nicht.
  fix [--base B] [--repo R] [--no-prs]
      Kollidierende NEUE ADRs auf die nächsten freien Nummern umbenennen (auch an
      offenen PRs vorbei): Dateiname, Titelzeile `# ADR NNNN`, Dateipfad-Verweise sowie
      `ADR NNNN` in vom Branch hinzugefügten Zeilen. ADRs, die auf B liegen, werden nie
      angefasst.

`check` vergleicht nur mit B (deterministisch, CI-tauglich). Offene PRs liest `gh`;
ohne `gh`/Netz gilt mit Warnung nur B.

Läuft im Paketverzeichnis (`fabrikant/`); Pfade sind relativ zum cwd.
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
PR_LIMIT = 200  # offene PRs, deren Dateien gelesen werden
# Laufzeitdaten/Umgebungen: `fix` schreibt dort nie (Run-Transkripte bleiben Historie).
SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", "runs", "state"})
ADR_FILE_RE = re.compile(r"^(\d{4})-.+\.md$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
NUMBER_WIDTH = 4


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def adr_number(path: str) -> int | None:
    """ADR-Nummer aus dem Dateinamen (`0013-foo.md` → 13), None für Nicht-ADRs (README)."""
    m = ADR_FILE_RE.match(Path(path).name)
    return int(m.group(1)) if m else None


def _new_adr_number(path: str) -> int:
    """Nummer eines Pfads aus `new_adrs` (dort auf ADRs gefiltert, also nie None)."""
    n = adr_number(path)
    if n is None:
        raise ValueError(f"kein ADR-Dateiname: {path}")
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
    """Im Branch neu hinzugefügte ADR-Dateien (sortiert nach Nummer, dann Name)."""
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
    """Neue ADRs mit bereits vergebener Nummer (auf base oder durch ein anderes neues ADR)."""
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
    """In offenen PRs belegte ADR-Nummern, ohne den PR des aktuellen Branches (sonst würde
    `fix` das eigene ADR immer weiter verschieben). PR-Pfade sind repo-relativ.

    Ohne `gh`/Netz: Warnung auf stderr, leere Menge (es gilt dann nur die Base)."""
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
    """Höchste vergebene Nummer + 1 (Lücken bleiben Lücken)."""
    return max(taken, default=0) + 1


def _stem(path: str) -> str:
    return Path(path).name.removesuffix(".md")


def renumbered_name(path: str, number: int) -> str:
    """Dateiname mit neuer Nummer (`0013-foo.md`, 14 → `0014-foo.md`)."""
    return f"{number:0{NUMBER_WIDTH}d}{Path(path).name[NUMBER_WIDTH:]}"


def added_lines(base: str) -> dict[str, set[int] | None]:
    """Vom Branch (inkl. Arbeitsstand) geänderte Dateien → hinzugefügte Zeilennummern.

    None = ganze Datei neu (alle Zeilen).

    Enthalten sind gegenüber der Merge-Base geänderte Dateien (committet, gestaget oder
    im Arbeitsstand) sowie ganze untracked/ignorierte Dateien (außer SKIP_DIRS);
    unveränderte getrackte Dateien bleiben unberührt. Bestehende ADRs der Base schließt
    ``fix`` zusätzlich aus.
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
    """Verweise auf umbenannte ADRs nachziehen.

    Dateinamen/Stems überall; `ADR NNNN` (alte Nummer) nur in hinzugefügten Zeilen
    (`lines`; None = alle) — der Rest der Datei gehört dem Stand auf base. Im
    umbenannten ADR selbst (`is_adr`) gilt das für alle Zeilen."""
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
    """Datei umbenennen; bei getrackten/gestageten Dateien per `git mv`, damit der Index konsistent bleibt."""
    if _git("ls-files", "--cached", "--", old_path).strip():
        _git("mv", old_path, new_path)
    else:
        Path(old_path).rename(new_path)


def fix(base: str, reserved: frozenset[int] = frozenset()) -> list[tuple[str, str]]:
    """Kollidierende neue ADRs umbenennen; liefert [(alter Pfad, neuer Pfad)].

    `reserved`: zusätzlich belegte Nummern (offene PRs), die neue Nummern überspringen."""
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
            continue  # bestehende ADRs (base) bleiben unberührt
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
            sp.add_argument("--no-prs", action="store_true", help="offene PRs nicht berücksichtigen")
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
            print(f"ADR-Nummer {adr_number(path):04d} kollidiert mit {args.base}: {path}")
        return 1 if clashing else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
