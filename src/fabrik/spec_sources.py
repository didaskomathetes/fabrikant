"""Spec sources: search path, spec in the target repo, register, path resolution (ADR 0047).

Order (first hit wins, name duplicates are reported by `fabrik validate`):
1. `FABRIK_PROJECTS_PATH` (colon-separated; folders with `*.fabrik.yaml` or
   target repo with `.fabrik/fabrik.yaml`)
2. `config.PROJECTS_DIR` (= `FABRIK_DATA/projects`)
3. target repos registered via `fabrik project add`
4. bundled examples (`config.BUNDLED_PROJECTS_DIR`)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import config
from .i18n import t

_SCRIPT_PATH_RE = re.compile(r"(?<![\w./-])(?:\./)?scripts/[\w./-]+")


def _dir_specs(d: Path) -> list[Path]:
    """Spec files of a folder (yaml before json, each sorted) or the target-repo spec."""
    found = [f for pattern in config.SPEC_GLOBS for f in sorted(d.glob(pattern))]
    target = d / config.TARGET_SPEC_RELPATH
    if target.is_file():
        found.append(target)
    return found


def registered_targets() -> list[Path]:
    """Per `register_target` bekannte Zielrepos (Reihenfolge der Registrierung)."""
    try:
        data = json.loads(config.REGISTRY_FILE.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):  # a corrupt registry must not cripple load_projects()
        return []
    return [Path(p) for p in data if isinstance(p, str)]


def register_target(path: str | Path) -> Path:
    """Register a target repo (idempotent); requires `.fabrik/fabrik.yaml`. Returns the spec file."""
    root = Path(path).expanduser().resolve()
    spec = root / config.TARGET_SPEC_RELPATH
    if not spec.is_file():
        raise ValueError(t("spec_sources.no_spec_found", spec=spec))
    known = [str(p) for p in registered_targets()]
    if str(root) not in known:
        registry = config.REGISTRY_FILE
        registry.parent.mkdir(parents=True, exist_ok=True)
        tmp = registry.with_suffix(".tmp")
        tmp.write_text(json.dumps([*known, str(root)], indent=2) + "\n")
        tmp.replace(registry)  # atomic: no half-written register after a crash
    return spec


def search_dirs() -> list[Path]:
    """All source folders in priority order (without duplicates, also non-existent ones)."""
    env = os.environ.get(config.PROJECTS_PATH_ENV, "")
    dirs = [Path(p).expanduser() for p in env.split(os.pathsep) if p.strip()]
    dirs += [config.PROJECTS_DIR, *registered_targets(), config.BUNDLED_PROJECTS_DIR]
    seen: set[Path] = set()
    unique = []
    for d in dirs:
        key = d.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def spec_files() -> list[Path]:
    """All spec files of all sources in priority order."""
    return [f for d in search_dirs() if d.is_dir() for f in _dir_specs(d)]


def default_name(path: Path) -> str:
    """Project name without `name:` — file name (`x.fabrik.yaml` → x), for a target-repo spec the repo folder."""
    if path.parent.name == config.TARGET_SPEC_RELPATH.parent.name:
        return path.parent.parent.name
    return path.stem.replace(".fabrik", "")


def resolve_relative(raw: str | Path, spec_path: str | Path | None) -> Path:
    """Resolve a relative path (e.g. `prompt_file`): first relative to the spec, then `FABRIK_HOME`.

    Absolute paths stay unchanged. If none of the candidates exists, the
    FABRIK_HOME path is returned (error message as before)."""
    p = Path(raw)
    if p.is_absolute():
        return p
    if spec_path:
        candidate = Path(spec_path).parent / p
        if candidate.is_file():
            return candidate
    return config.FABRIK_HOME / p


def resolve_script_paths(command: str, spec_path: str | Path | None) -> str:
    """Resolve ``scripts/...`` references in a shell command relative to its spec.

    Existing paths are made absolute so the shell step can retain the project's
    worktree as its working directory. Missing paths remain unchanged so the
    shell reports the same command-not-found error as before.
    """

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        path = resolve_relative(raw.removeprefix("./"), spec_path)
        return str(path) if path.is_file() else raw

    return _SCRIPT_PATH_RE.sub(replace, command)
