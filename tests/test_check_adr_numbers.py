"""scripts/check-adr-numbers.py — Kollisionen, next, fix gegen ein echtes Temp-Git-Repo; offene PRs (gh gemockt)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from helpers import load_script

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-adr-numbers.py"


@pytest.fixture(scope="module")
def mod():
    return load_script(SCRIPT.name, "check_adr_numbers")


@pytest.fixture(autouse=True)
def open_prs(mod, monkeypatch):
    """Offene PRs als Liste `{headRefName, files}` — Default: keine (nie echtes gh/Netz)."""
    prs: list[dict] = []
    monkeypatch.setattr(mod, "_gh_open_prs", lambda repo: prs)
    return prs


def _pr(branch: str, *paths: str) -> dict:
    return {"headRefName": branch, "files": [{"path": p} for p in paths]}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Repo mit Unterverzeichnis `pkg/` (wie fabrik/), main hat ADRs 1, 2, 5 (Lücke); Branch `feat`.
    `origin/main` ist eine lokale Ref auf main."""
    pkg = tmp_path / "pkg"
    adr = pkg / "docs" / "adr"
    adr.mkdir(parents=True)
    for n, name in ((1, "alpha"), (2, "beta"), (5, "gamma")):
        (adr / f"{n:04d}-{name}.md").write_text(f"# ADR {n:04d}: {name}\n")
    (adr / "README.md").write_text("| [0005](0005-gamma.md) |\n")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "main")
    _git(tmp_path, "checkout", "-b", "feat")
    monkeypatch.chdir(pkg)
    return pkg


def _add(repo: Path, name: str, text: str | None = None) -> None:
    n = name[:4]
    (repo / "docs" / "adr" / name).write_text(text or f"# ADR {n}: x\n")


def test_no_new_adr_no_collision(mod, repo):
    assert mod.collisions("origin/main") == []
    assert mod.main(["check"]) == 0


def test_next_ignores_gaps(mod, repo, capsys):
    mod.main(["next"])
    assert capsys.readouterr().out.strip() == "0006"


def test_single_collision_detected(mod, repo, capsys):
    _add(repo, "0005-new.md")
    assert mod.main(["check"]) == 1
    assert "docs/adr/0005-new.md" in capsys.readouterr().out


def test_staged_collision_detected(mod, repo, capsys):
    _add(repo, "0005-staged.md")
    _git(repo, "add", "docs/adr/0005-staged.md")
    assert mod.main(["check"]) == 1
    assert "docs/adr/0005-staged.md" in capsys.readouterr().out


def test_gap_number_is_free(mod, repo):
    _add(repo, "0003-fills-gap.md")  # 3 ist auf main nicht vergeben
    assert mod.collisions("origin/main") == []


def test_committed_branch_with_head(mod, repo):
    _add(repo, "0002-new.md")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "adr")
    _git(repo, "update-ref", "refs/heads/head-ref", "HEAD")
    assert mod.collisions("origin/main", "head-ref") == ["docs/adr/0002-new.md"]


def test_fix_renumbers_consecutively_and_updates_references(mod, repo):
    _add(repo, "0005-one.md", "# ADR 0005: one\nSiehe ADR 0005.\n")
    _add(repo, "0001-two.md", "# ADR 0001: two\n")
    (repo / "README.md").write_text("alt\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "readme")
    (repo / "README.md").write_text("alt\nSiehe ADR 0005 und 0005-one.md\n")
    (repo / "docs" / "adr" / "README.md").write_text("| [0005](0005-gamma.md) |\n| [0005](0005-one.md) |\n")

    moves = mod.fix("origin/main")

    assert sorted(new for _, new in moves) == ["docs/adr/0006-two.md", "docs/adr/0007-one.md"]
    one = (repo / "docs/adr/0007-one.md").read_text()
    assert one == "# ADR 0007: one\nSiehe ADR 0007.\n"
    assert (repo / "docs/adr/0006-two.md").read_text() == "# ADR 0006: two\n"
    assert (repo / "README.md").read_text() == "alt\nSiehe ADR 0007 und 0007-one.md\n"
    index = (repo / "docs/adr/README.md").read_text()
    assert "[0005](0005-gamma.md)" in index and "[0007](0007-one.md)" in index
    assert mod.collisions("origin/main") == []


def test_fix_keeps_base_adrs_and_old_references_untouched(mod, repo):
    (repo / "docs" / "adr" / "0002-beta.md").write_text("# ADR 0002: beta\nbearbeitet\n")  # Base-ADR geändert
    _git(repo, "stash", "push", "-u", "-m", "adr-test-wip")
    _git(repo, "checkout", "main")
    (repo / "NOTES.md").write_text("Alt ADR 0005 (main).\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "notes")
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    _git(repo, "checkout", "feat")
    _git(repo, "rebase", "main")
    _git(repo, "stash", "pop")
    _add(repo, "0005-new.md")
    (repo / "NOTES.md").write_text("Alt ADR 0005 (main).\nNeu ADR 0005.\n")

    mod.fix("origin/main")

    assert (repo / "NOTES.md").read_text() == "Alt ADR 0005 (main).\nNeu ADR 0006.\n"
    assert (repo / "docs/adr/0005-gamma.md").read_text() == "# ADR 0005: gamma\n"
    assert (repo / "docs/adr/0002-beta.md").read_text() == "# ADR 0002: beta\nbearbeitet\n"


def test_fix_without_collision_is_noop(mod, repo, capsys):
    _add(repo, "0009-fine.md")
    assert mod.main(["fix"]) == 0
    assert capsys.readouterr().out == ""
    assert (repo / "docs/adr/0009-fine.md").exists()


def test_duplicate_numbers_among_new_adrs(mod, repo):
    _add(repo, "0009-a.md")
    _add(repo, "0009-b.md")
    assert mod.collisions("origin/main") == ["docs/adr/0009-b.md"]
    mod.fix("origin/main")
    assert (repo / "docs/adr/0010-b.md").exists() and (repo / "docs/adr/0009-a.md").exists()


def test_fix_renumbers_staged_collision(mod, repo):
    _add(repo, "0005-staged.md", "# ADR 0005: staged\n")
    _git(repo, "add", "docs/adr/0005-staged.md")
    assert "docs/adr/0005-staged.md" in mod.added_lines("origin/main")

    moves = mod.fix("origin/main")

    assert moves == [("docs/adr/0005-staged.md", "docs/adr/0006-staged.md")]
    assert not (repo / "docs/adr/0005-staged.md").exists()
    assert (repo / "docs/adr/0006-staged.md").read_text() == "# ADR 0006: staged\n"
    assert mod.main(["check"]) == 0  # fix → check: Index zeigt keine alte Nummer mehr


def test_fix_updates_ignored_reference_file(mod, repo):
    (repo / ".gitignore").write_text("generated/\n")
    generated = repo / "generated"
    generated.mkdir()
    reference = generated / "report.md"
    reference.write_text("Siehe ADR 0005 und 0005-new.md\n")
    _add(repo, "0005-new.md", "# ADR 0005: new\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore generated reports")

    mod.fix("origin/main")

    assert reference.read_text() == "Siehe ADR 0006 und 0006-new.md\n"


# ---------------------------------------------------------------- offene PRs (Umnummerierung 2026-10-20)
def test_next_counts_open_prs_but_not_own_branch_or_other_dirs(mod, repo, open_prs, capsys):
    open_prs += [
        _pr("other", "pkg/docs/adr/0008-other.md", "pkg/src/x.py"),
        _pr("feat", "pkg/docs/adr/0012-mine.md"),  # PR des aktuellen Branches
        _pr("elsewhere", "otherproject/docs/adr/0040-x.md", "pkg/docs/adr/README.md"),
    ]
    mod.main(["next"])
    assert capsys.readouterr().out.strip() == "0009"
    mod.main(["next", "--no-prs"])
    assert capsys.readouterr().out.strip() == "0006"


def test_next_without_gh_falls_back_to_base(mod, repo, monkeypatch, capsys):
    def boom(repo):
        raise subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(mod, "_gh_open_prs", boom)
    mod.main(["next"])
    out = capsys.readouterr()
    assert out.out.strip() == "0006" and "Warnung" in out.err


def test_fix_skips_numbers_reserved_by_open_prs(mod, repo, open_prs):
    open_prs.append(_pr("other", "pkg/docs/adr/0006-other.md", "pkg/docs/adr/0007-other2.md"))
    _add(repo, "0005-new.md")
    assert mod.main(["fix"]) == 0
    assert (repo / "docs/adr/0008-new.md").exists()


def test_fix_never_rewrites_runtime_dirs(mod, repo):
    """Run-Transkripte (runs/, ignoriert) bleiben Historie, auch wenn sie die alte Nummer nennen."""
    (repo / ".gitignore").write_text("runs/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore runs")
    record = repo / "runs" / "p" / "r.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"log": "ADR 0005 und 0005-new.md"}))
    _add(repo, "0005-new.md")
    mod.fix("origin/main")
    assert "ADR 0005 und 0005-new.md" in record.read_text()


def test_gh_open_prs_parses_json(mod, monkeypatch):
    monkeypatch.undo()  # echtes _gh_open_prs, nur subprocess gemockt
    calls = []

    def fake_run(cmd, check, capture_output, text):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([_pr("b", "pkg/docs/adr/0001-x.md")]))

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod._gh_open_prs("o/r") == [_pr("b", "pkg/docs/adr/0001-x.md")]
    assert calls[0][:5] == ["gh", "pr", "list", "--repo", "o/r"]
