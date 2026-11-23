"""Template library (ADR 0067): list, inspect, and scaffold specs from loop templates.

Templates follow the ADR 0047 format (`params:` + `loop:`) and are resolved like
`spec_expand.find_template`: the user dir (`FABRIKANT_DATA/templates`) overrides the
bundled dir (the package's ``templates/`` folder) when names collide.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import config
from .i18n import t
from .spec_expand import _NAME_RE, _load_template


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


def list_templates() -> list[dict]:
    """Every template with description and parameter summary (for `fabrikant template list`).

    A structurally broken template is not hidden and does not hide the others: it is listed
    with an ``error`` message (and no parameter summary).
    """
    out = []
    for name, path in template_files().items():
        source = "user" if path.parent == config.USER_TEMPLATES_DIR else "bundled"
        try:
            data = _load_template(name, None)
        except (ValueError, OSError, yaml.YAMLError) as e:
            out.append({"name": name, "path": path, "source": source, "error": str(e)})
            continue
        loop = data["loop"]
        params = data.get("params") or {}
        out.append(
            {
                "name": name,
                "path": path,
                "source": source,
                "description": loop.get("description", ""),
                "required": sorted(k for k, v in params.items() if v is None),
                "defaults": sorted(k for k, v in params.items() if v is not None),
            }
        )
    return out


def scaffold_spec(template: str, project: str, out_dir: str | Path, force: bool = False) -> Path:
    """Write `<out_dir>/<project>.fabrikant.yaml` with one loop instance of `template`.

    Required template parameters (`null` defaults) are written as `param: null  # TODO`;
    everything else comes from the template's defaults. The result is valid YAML but
    intentionally fails `fabrikant validate` until the TODOs are filled in.
    """
    if not isinstance(project, str) or not _NAME_RE.fullmatch(project):
        raise ValueError(t("templates.project_name_invalid", name=project))
    data = _load_template(template, None)  # unknown template → ValueError
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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
