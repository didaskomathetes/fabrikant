#!/usr/bin/env python3
"""ci-matrix-pytest — die pytest-Matrix eines GitHub-Workflows lokal ausführen.

Für Projekte, deren Tests pro Paket in einer CI-Matrix laufen (z. B. ein uv-Monorepo mit
`.github/workflows/python.yml`, Job `python-test`).
Die Matrix ist die einzige Quelle — das Script liest `include`-Einträge
(`package`, `test_home`) und das `env` des pytest-Steps, statt sie zu duplizieren.

Ausgewählt werden nur Einträge, deren Komponente der Diff gegen `--base` berührt
(Komponente = die ersten zwei Pfadsegmente von `test_home`, z. B. `src/mcp`).
Änderungen an geteilten Pfaden (`--shared`, Default: `libs/`, `uv.lock`,
`pyproject.toml`) wählen die ganze Matrix. CI bleibt die Autorität; das hier ist
der schnelle lokale Gate vor dem Push.

    ci-matrix-pytest.py [--workflow W] [--job J] [--base origin/main | --all] [--cov-dir D] [--dry-run]

`--all` wählt die ganze Matrix (z. B. Sonar-Scan auf main). `--cov-dir D` misst Coverage für
SonarQube: Pakete werden dann *editable* gesynct (sonst zeigen die Pfade nach site-packages statt
auf die Quellen), pytest-cov wird nachinstalliert (nicht im Lockfile) und je Eintrag entsteht
`D/coverage-<n>.xml` (Cobertura) für die Komponente.

Exit-Code: 0 = alle ausgewählten Pakete grün (oder keins betroffen), sonst 1.
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
COMPONENT_DEPTH = 2  # src/<komponente>/… bzw. libs/<lib>/…
GH_EXPRESSION = "${{"  # Werte mit GitHub-Expressions sind lokal nicht auflösbar


@dataclass(frozen=True)
class MatrixEntry:
    """Ein Matrix-Eintrag: uv-Workspace-Paket + Verzeichnis, in dem pytest läuft."""

    package: str
    test_home: str

    @property
    def component(self) -> str:
        return "/".join(Path(self.test_home).parts[:COMPONENT_DEPTH])


@dataclass
class TestMatrix:
    """Matrix-Einträge + Umgebung des pytest-Steps aus dem Workflow."""

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
        """Einträge, deren Komponente ein geänderter Pfad berührt; geteilte Pfade → alle."""
        if any(p.startswith(s) for p in changed for s in shared):
            return list(self.entries)
        return [e for e in self.entries if any(p == e.component or p.startswith(e.component + "/") for p in changed)]


def changed_paths(base: str, cwd: Path) -> list[str]:
    """Geänderte Pfade gegen den Merge-Base mit `base`, inkl. ungestagter/untracked."""

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
    """Paket wie in CI syncen und pytest im test_home ausführen.

    `cov_xml`: Coverage der Komponente als Cobertura-XML (editable Sync + pytest-cov, s. Modul-Doku)."""
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
    p.add_argument("--all", action="store_true", help="ganze Matrix statt nur betroffener Einträge")
    p.add_argument("--cov-dir", type=Path, help="Coverage-XML je Eintrag hierhin (für SonarQube)")
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--dry-run", action="store_true", help="nur betroffene Einträge auflisten")
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
        # Absolut: pytest läuft im test_home des Eintrags — ein relativer Pfad landete dort statt im Repo.
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
