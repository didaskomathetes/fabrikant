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

Exit code: 0 = all selected packages green (or none affected), otherwise 1.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_WORKFLOW = ".github/workflows/python.yml"
DEFAULT_JOB = "python-test"
DEFAULT_BASE = "origin/main"
DEFAULT_SHARED = ("libs/", "uv.lock", "pyproject.toml")
COMPONENT_DEPTH = 2  # src/<component>/… or libs/<lib>/…
GH_EXPRESSION = "${{"  # values with GitHub expressions cannot be resolved locally


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


def run_entry(entry: MatrixEntry, env: dict[str, str], root: Path, cov_xml: Path | None = None) -> bool:
    """Sync the package as in CI and run pytest in test_home.

    `cov_xml`: coverage of the component as Cobertura XML (editable sync + pytest-cov, see module docs)."""
    print(f"== {entry.package} ({entry.test_home})", flush=True)
    sync = ["uv", "sync", "--frozen", "--package", entry.package, "--no-dev", "--extra", "test"]
    if cov_xml is None:
        sync.insert(5, "--no-editable")
    if subprocess.run(sync, cwd=root).returncode != 0:
        return False
    python = root / ".venv" / "bin" / "python"
    pytest = [str(python), "-m", "pytest", "-q"]
    if cov_xml is not None:
        install = ["uv", "pip", "install", "--quiet", "--python", str(python), COV_PACKAGE]
        if subprocess.run(install, cwd=root).returncode != 0:
            return False
        pytest += [f"--cov={root / entry.component}", f"--cov-report=xml:{cov_xml}"]
    proc = subprocess.run(pytest, cwd=root / entry.test_home, env={**os.environ, **env})
    return proc.returncode == 0


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
    failed = [
        e.test_home
        for i, e in enumerate(selected)
        if not run_entry(e, matrix.env, args.root, args.cov_dir / f"coverage-{i}.xml" if args.cov_dir else None)
    ]
    if failed:
        print(f"rot: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"grün: {len(selected)} Paket(e)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
