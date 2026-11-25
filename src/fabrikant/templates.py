"""Template library (ADR 0067): list, inspect, and scaffold specs from loop templates.

Templates follow the ADR 0047 format (`params:` + `loop:`) and are resolved like
`spec_expand.find_template`: the user dir (`FABRIKANT_DATA/templates`) overrides the
bundled dir (the package's ``templates/`` folder) when names collide.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config
from .i18n import t
from .spec_expand import is_valid_name, load_template

_FILE_MODE = 0o644  # mkstemp creates 0600; scaffolded specs are normal repo files


def template_dirs() -> list[Path]:
    """Template folders in priority order (user dir first — it overrides bundled)."""
    return [config.USER_TEMPLATES_DIR, config.TEMPLATES_DIR]


def template_files() -> dict[str, Path]:
    """Name → template file for every template (same name: user dir wins)."""
    found: dict[str, Path] = {}
    for d in reversed(template_dirs()):
        if not d.is_dir():
            continue
        for f in sorted(d.glob(f"*{config.TEMPLATE_SUFFIX}")):
            found[f.name[: -len(config.TEMPLATE_SUFFIX)]] = f
    return found


@dataclass(frozen=True)
class TemplateInfo:
    """One `template list` entry; ``error`` is set (and the summary empty) for a broken template."""

    name: str
    path: Path
    source: str  # "user" | "bundled"
    description: str = ""
    required: list[str] = field(default_factory=list)
    defaults: list[str] = field(default_factory=list)
    error: str | None = None


def list_templates() -> list[TemplateInfo]:
    """Every template with description and parameter summary (for `fabrikant template list`).

    A structurally broken template is not hidden and does not hide the others: it is listed
    with an ``error`` message (and no parameter summary).
    """
    out = []
    for name, path in template_files().items():
        source = "user" if path.parent == config.USER_TEMPLATES_DIR else "bundled"
        try:
            data = load_template(name, None)
            loop = data["loop"]
            params = data.get("params") or {}
            required = sorted(k for k, v in params.items() if v is None)
            defaults = sorted(k for k, v in params.items() if v is not None)
        except (ValueError, OSError, yaml.YAMLError, AttributeError, TypeError) as e:
            out.append(TemplateInfo(name, path, source, error=str(e)))
            continue
        out.append(TemplateInfo(name, path, source, str(loop.get("description", "")), required, defaults))
    return out


def _write_atomic(path: Path, text: str, overwrite: bool) -> None:
    """Write `text` via temp file in the same dir; never follows a symlink at `path`.

    ``overwrite``: `os.replace` (swaps a symlink itself out). Otherwise `os.link` — atomic
    create-if-absent, so a concurrent writer cannot be clobbered (FileExistsError).
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, _FILE_MODE)
        if overwrite:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def scaffold_spec(template: str, project: str, out_dir: str | Path, force: bool = False) -> Path:
    """Write `<out_dir>/<project>.fabrikant.yaml` with one loop instance of `template`.

    Required template parameters (`null` defaults) are written as `param: null  # TODO`;
    everything else comes from the template's defaults. The result is valid YAML but
    intentionally fails `fabrikant validate` until the TODOs are filled in.
    """
    if not is_valid_name(project):
        raise ValueError(t("templates.project_name_invalid", name=project))
    if not project[0].isalnum():  # a leading `-` would read as a CLI option
        raise ValueError(t("templates.project_name_invalid_start", name=project))
    data = load_template(template, None)  # unknown template → ValueError
    params = data.get("params") or {}
    out = Path(out_dir).expanduser()
    path = out / f"{project}.fabrikant.yaml"
    if path.exists() and not force:
        raise FileExistsError(t("templates.file_exists_use_force", path=path))
    lines = [
        f"# {project} — scaffolded from template `{template}` (`fabrikant template create`).",
        "# Fill in the TODOs, then check with `fabrikant validate`.",
        f"name: {project}",
        f'description: "{project}"',
        "# root: /path/to/repo  # TODO: repository root of the project",
        "loops:",
        f"  {template}:",
        f"    use: {template}",
        "    with:",
    ]
    required = sorted(k for k, v in params.items() if v is None)
    if required:
        for key in required:
            lines.append(f"      {key}: null  # TODO: required (no template default)")
    else:
        lines.append("      {}")  # no required params — empty mapping
    out.mkdir(parents=True, exist_ok=True)
    try:
        _write_atomic(path, "\n".join(lines) + "\n", overwrite=force)
    except FileExistsError:
        raise FileExistsError(t("templates.file_exists_use_force", path=path)) from None
    return path
