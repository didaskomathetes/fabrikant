#!/usr/bin/env python3
"""ci-matrix-pytest — run the pytest matrix of a GitHub workflow locally.

For projects whose tests run per package in a CI matrix (e.g. a uv monorepo with
`.github/workflows/python.yml`, job `python-test`).
The matrix is the only source — the script reads the `include` entries
(`package`, `test_home`) and the `env` of the pytest step instead of duplicating them.

Only entries whose component is touched by the diff against `--base` are selected
(component = the first two path segments of `test_home`, e.g. `src/mcp`).
Changes to shared paths (`--shared`, default: `libs/`, `uv.lock`,
`pyproject.toml`) select the whole matrix. CI remains the authority; this is
the fast local gate before pushing.

    ci-matrix-pytest.py [--workflow W] [--job J] [--base origin/main | --all] [--cov-dir D] [--dry-run]

`--all` selects the whole matrix (e.g. Sonar scan on main). `--cov-dir D` measures coverage for
SonarQube: packages are then synced *editable* (otherwise the paths point to site-packages instead of
the sources), pytest-cov is installed on top (not in the lockfile) and each entry produces
`D/coverage-<n>.xml` (Cobertura) for the component.

If pytest dies from a signal (e.g. SIGSEGV while loading native libs in the sandbox), that is a
crash, not a test result: the entry runs once more (`CRASH_RERUNS`). If it still crashes, the
script exits with 75 (EX_TEMPFAIL → failure class `transient`, ADR 0035) — the run retry kicks in
instead of discarding the builder's work as "tests red".

Exit code: 0 = all selected packages green (or none affected), 1 = at least one package red,
75 = no package red, but at least one crashed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import yaml

DEFAULT_WORKFLOW = ".github/workflows/python.yml"
DEFAULT_JOB = "python-test"
DEFAULT_BASE = "origin/main"
DEFAULT_SHARED = ("libs/", "uv.lock", "pyproject.toml")
COMPONENT_DEPTH = 2  # src/<component>/… or libs/<lib>/…
GH_EXPRESSION = "${{"  # values with GitHub expressions cannot be resolved locally
EXIT_RED = 1
EX_TEMPFAIL = 75  # sysexits.h; fabrikant classifies this as `transient` (src/fabrikant/failures.py)
CRASH_RERUNS = 1  # restarts of a crashed entry before it counts as crashed
_SIGNAL_EXIT_BASE = 128  # shell convention: death by signal n → exit 128+n
_MAX_SIGNAL = 64


class Outcome(StrEnum):
    """Result of a matrix entry."""

    PASS = "pass"
    FAIL = "fail"  # sync/install failed or pytest reports red tests
    CRASH = "crash"  # pytest died from a signal — no test result


def is_signal_exit(code: int) -> bool:
    """True if the process died from a signal (``subprocess``: -n, shell: 128+n)."""
    return code < 0 or _SIGNAL_EXIT_BASE < code <= _SIGNAL_EXIT_BASE + _MAX_SIGNAL


@dataclass(frozen=True)
class MatrixEntry:
    """A matrix entry: uv workspace package + directory in which pytest runs."""

    package: str
    test_home: str

    @property
    def component(self) -> str:
        return "/".join(Path(self.test_home).parts[:COMPONENT_DEPTH])


@dataclass
class TestMatrix:
    """Matrix entries + environment of the pytest step from the workflow."""

    entries: list[MatrixEntry]
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_workflow(cls, workflow: dict, job: str) -> TestMatrix:
        jobdef = (workflow.get("jobs") or {}).get(job)
        if not jobdef:
            raise ValueError(f"Job {job!r} fehlt im Workflow")
        include = ((jobdef.get("strategy") or {}).get("matrix") or {}).get("include") or []
        entries = [MatrixEntry(str(e["package"]), str(e["test_home"])) for e in include]
        if not entries:
            raise ValueError(f"Job {job!r}: keine matrix.include-Einträge mit package/test_home")
        step = next((s for s in jobdef.get("steps") or [] if "pytest" in str(s.get("run", ""))), {})
        env = {k: str(v) for k, v in (step.get("env") or {}).items() if GH_EXPRESSION not in str(v)}
        return cls(entries, env)

    def affected(self, changed: list[str], shared: tuple[str, ...] = DEFAULT_SHARED) -> list[MatrixEntry]:
        """Entries whose component is touched by a changed path; shared paths → all."""
        if any(p.startswith(s) for p in changed for s in shared):
            return list(self.entries)
        return [e for e in self.entries if any(p == e.component or p.startswith(e.component + "/") for p in changed)]


def changed_paths(base: str, cwd: Path) -> list[str]:
    """Changed paths against the merge base with `base`, incl. unstaged/untracked."""

    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout
        return [line for line in out.splitlines() if line]

    return sorted(
        set(git("diff", "--name-only", f"{base}...HEAD"))
        | set(git("diff", "--name-only", "HEAD"))
        | set(git("ls-files", "--others", "--exclude-standard"))
    )


COV_PACKAGE = "pytest-cov"


def run_entry(entry: MatrixEntry, env: dict[str, str], root: Path, cov_xml: Path | None = None) -> Outcome:
    """Sync the package as in CI and run pytest in test_home.

    `cov_xml`: coverage of the component as Cobertura XML (editable sync + pytest-cov, see module docs)."""
    print(f"== {entry.package} ({entry.test_home})", flush=True)
    sync = ["uv", "sync", "--frozen", "--package", entry.package, "--no-dev", "--extra", "test"]
    if cov_xml is None:
        sync.insert(5, "--no-editable")
    if subprocess.run(sync, cwd=root).returncode != 0:
        return Outcome.FAIL
    python = root / ".venv" / "bin" / "python"
    pytest = [str(python), "-m", "pytest", "-q"]
    if cov_xml is not None:
        install = ["uv", "pip", "install", "--quiet", "--python", str(python), COV_PACKAGE]
        if subprocess.run(install, cwd=root).returncode != 0:
            return Outcome.FAIL
        pytest += [f"--cov={root / entry.component}", f"--cov-report=xml:{cov_xml}"]
    proc = subprocess.run(pytest, cwd=root / entry.test_home, env={**os.environ, **env})
    if proc.returncode == 0:
        return Outcome.PASS
    return Outcome.CRASH if is_signal_exit(proc.returncode) else Outcome.FAIL


def run_entry_with_rerun(entry: MatrixEntry, env: dict[str, str], root: Path, cov_xml: Path | None = None) -> Outcome:
    """:func:`run_entry`; a crash is retried up to ``CRASH_RERUNS`` times (red tests never)."""
    outcome = run_entry(entry, env, root, cov_xml)
    for attempt in range(CRASH_RERUNS):
        if outcome is not Outcome.CRASH:
            break
        print(f"⚠ {entry.test_home}: pytest abgestürzt (Signal) — Neustart {attempt + 1}/{CRASH_RERUNS}", flush=True)
        outcome = run_entry(entry, env, root, cov_xml)
    return outcome


def exit_status(outcomes: list[tuple[str, Outcome]]) -> int:
    """Exit code of the matrix from ``(test_home, outcome)``: red beats crashed (a real result
    counts), otherwise crashed → 75, otherwise 0."""
    failed = [home for home, o in outcomes if o is Outcome.FAIL]
    crashed = [home for home, o in outcomes if o is Outcome.CRASH]
    if crashed:
        print(f"abgestürzt: {', '.join(crashed)}", file=sys.stderr)
    if failed:
        print(f"rot: {', '.join(failed)}", file=sys.stderr)
        return EXIT_RED
    if crashed:
        return EX_TEMPFAIL
    print(f"grün: {len(outcomes)} Paket(e)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    p.add_argument("--job", default=DEFAULT_JOB)
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--all", action="store_true", help="whole matrix instead of only affected entries")
    p.add_argument("--cov-dir", type=Path, help="write coverage XML per entry here (for SonarQube)")
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--dry-run", action="store_true", help="only list affected entries")
    args = p.parse_args(argv)

    matrix = TestMatrix.from_workflow(yaml.safe_load((args.root / args.workflow).read_text()), args.job)
    selected = list(matrix.entries) if args.all else matrix.affected(changed_paths(args.base, args.root))
    if not selected:
        print("keine Test-Komponente betroffen — nichts zu tun")
        return 0
    if args.dry_run:
        for e in selected:
            print(f"{e.package}\t{e.test_home}")
        return 0
    if args.cov_dir:
        # Absolute: pytest runs in the entry's test_home — a relative path would land there instead of in the repo.
        args.cov_dir = args.cov_dir.resolve()
        args.cov_dir.mkdir(parents=True, exist_ok=True)
    outcomes = [
        (
            e.test_home,
            run_entry_with_rerun(
                e, matrix.env, args.root, args.cov_dir / f"coverage-{i}.xml" if args.cov_dir else None
            ),
        )
        for i, e in enumerate(selected)
    ]
    return exit_status(outcomes)


if __name__ == "__main__":
    sys.exit(main())
