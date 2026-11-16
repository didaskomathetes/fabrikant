"""ADR-Invarianten des Arbeitsstands: Nummern eindeutig, Header passt zum Dateinamen,
Index vollständig + sortiert, Verweise zeigen auf vorhandene ADRs.

Nummernvergabe/-reparatur: scripts/check-adr-numbers.py (tests/test_check_adr_numbers.py)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = ROOT / "docs" / "adr"
_FILE_RE = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
_ROW_RE = re.compile(r"^\| \[(\d{4})\]\(([^)]+)\) \|", re.M)


def _adr_files() -> list[Path]:
    return sorted(p for p in ADR_DIR.glob("*.md") if p.name != "README.md")


def test_adr_filenames_follow_convention():
    bad = [p.name for p in _adr_files() if not _FILE_RE.match(p.name)]
    assert not bad, f"Dateiname nicht `NNNN-kurz-titel.md`: {bad}"


def test_adr_numbers_are_unique():
    """Parallele Branches dürfen keine Nummer doppelt vergeben (Umnummerierung 2026-10-20)."""
    seen: dict[str, str] = {}
    dupes = []
    for p in _adr_files():
        num = p.name[:4]
        if num in seen:
            dupes.append(f"{num}: {seen[num]} + {p.name}")
        seen[num] = p.name
    assert not dupes, "ADR-Nummer doppelt — `python scripts/check-adr-numbers.py fix` nutzen: " + "; ".join(dupes)


def test_adr_header_matches_filename():
    bad = []
    for p in _adr_files():
        first = p.read_text().splitlines()[0]
        if not first.startswith(f"# ADR {p.name[:4]}:"):
            bad.append(f"{p.name}: {first!r}")
    assert not bad, f"Header-Nummer ≠ Dateiname: {bad}"


def test_adr_index_lists_every_adr_once_in_order():
    rows = _ROW_RE.findall((ADR_DIR / "README.md").read_text())
    links = [link for _, link in rows]
    assert links == [p.name for p in _adr_files()], "docs/adr/README.md: Index unvollständig/unsortiert"
    assert all(link.startswith(num) for num, link in rows), "Index-Nummer ≠ verlinkte Datei"


_REF_RE = re.compile(r"\bADR[ -]?(\d{4})((?:/\d{4})*)")
# Wo ADR-Verweise stehen; runs/ und state/ sind Laufzeitdaten.
_REF_SOURCES = (
    "README.md",
    "fabrik.env.example",
    "deploy",
    "docs",
    "projects",
    "prompts",
    "scripts",
    "src",
    "tests",
    "web",
)
# Fixture-ADRs eines Temp-Repos, keine echten Verweise.
_REF_EXEMPT = frozenset({"tests/test_check_adr_numbers.py"})


def test_adr_references_point_to_existing_adrs():
    """`ADR NNNN` (auch `ADR NNNN/MMMM`) muss auf eine vorhandene ADR zeigen — fängt Verweise
    auf vergebene-und-wieder-verschobene Nummern (z. B. 0007). Ob der Verweis die *richtige*
    ADR meint, prüft das nicht."""
    existing = {p.name[:4] for p in _adr_files()}
    bad = []
    for src in _REF_SOURCES:
        base = ROOT / src
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        for f in files:
            if f.relative_to(ROOT).as_posix() in _REF_EXEMPT:
                continue
            try:
                text = f.read_text()
            except UnicodeDecodeError:
                continue  # Binärdateien
            for i, line in enumerate(text.splitlines(), 1):
                for m in _REF_RE.finditer(line):
                    nums = [m.group(1), *filter(None, m.group(2).split("/"))]
                    bad += [f"{f.relative_to(ROOT)}:{i}: ADR {n}" for n in nums if n not in existing]
    assert not bad, "Verweis auf nicht existierende ADR:\n" + "\n".join(bad)
