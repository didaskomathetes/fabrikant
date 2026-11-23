"""Spec loading, validation & saving (sources: spec_sources, vars/templates: spec_expand; ADR 0047).

Saving (:func:`save_spec`) uses ``ruamel.yaml`` in round-trip mode so that
comments and order of the untouched file parts are preserved."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator
from ruamel.yaml import YAML

from .i18n import t as _t

try:
    import yaml
except ImportError:
    yaml = None

from . import alerts, billing, branches, config, slack, spec_sources, specschema, webhook
from .backends import known_backends
from .failures import SPEC_FAILURE_KINDS
from .gitops import GH_TOKEN_ENV, SPEC_ENV_FILE_KEY
from .hosting import registered_providers
from .hosting.select import HOSTING_KEY
from .relay.config import expand_loops as expand_relay_loops
from .runs import LOCK_KEY_SEP
from .spec_expand import expand_spec
from .steps import extra_context
from .steps import registry as step_registry


class SpecConflict(ValueError):
    """Optimistic-lock violation: the spec was changed externally since the draft was loaded
    (``base_hash`` ≠ current file hash). The client should reload the
    draft instead of overwriting the foreign changes (→ HTTP 409)."""


def spec_path(name: str, projects: dict | None = None) -> Path:
    """Return the source path for a loaded project, across all spec sources.

    Callers that already hold `load_projects()` pass it as `projects` (avoids a reload)."""
    spec = (load_projects() if projects is None else projects).get(name)
    if spec and spec.get("_path"):
        return Path(spec["_path"])
    return config.PROJECTS_DIR / f"{name}.fabrikant.yaml"


# Top-level fields editable via PUT /api/projects/<name>/spec.
# Whitelist instead of a blank check: `name`/`root`/`loops`/`vars` & co. stay
# untouched (name is coupled to the file name, `vars` to the expansion).
SPEC_EDITABLE_FIELDS = ("description", "max_parallel")


@dataclass
class SpecUpdate:
    """Save update for a project (``PUT /api/projects/<name>/spec``).

    ``loops`` contains the loops to save (None = unchanged) — they are merged
    per loop (unknown keys of the existing definition are kept),
    not replacing the whole section. ``remove_loops`` names loops that are
    deleted. ``project_fields`` updates single top-level fields from
    :data:`SPEC_EDITABLE_FIELDS`."""

    loops: dict | None = None
    remove_loops: list[str] = field(default_factory=list)
    project_fields: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict) -> SpecUpdate:
        """JSON request body → :class:`SpecUpdate`; structurally invalid → ``ValueError``.

        Unknown fields are rejected (typo protection), so a
        ``max_paralell`` field is not silently ignored."""
        loops = payload.get("loops")
        if loops is not None and not isinstance(loops, dict):
            raise ValueError(_t("specs.loops_must_be_an_object"))
        remove = payload.get("remove_loops") or []
        if not isinstance(remove, list) or not all(isinstance(n, str) for n in remove):
            raise ValueError(_t("specs.remove_loops_must_be_a"))
        fields: dict[str, object] = {}
        for key in SPEC_EDITABLE_FIELDS:
            if key not in payload:
                continue
            value = payload[key]
            if key == "description" and not isinstance(value, str):
                raise ValueError(_t("specs.description_must_be_a_string"))
            if key == "max_parallel" and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(_t("specs.max_parallel_must_be_an"))
            fields[key] = value
        unknown = sorted(set(payload) - {"loops", "remove_loops", *SPEC_EDITABLE_FIELDS})
        if unknown:
            raise ValueError(
                f"unbekannte Felder: {', '.join(unknown)} (erlaubt: loops, remove_loops, "
                f"{', '.join(SPEC_EDITABLE_FIELDS)})"
            )
        return cls(loops=loops, remove_loops=list(remove), project_fields=fields)


def _roundtrip_yaml() -> YAML:
    """ruamel YAML instance in round-trip mode (comments, order and
    quotes of the untouched file parts are preserved).

    ``width=4096`` prevents line-wrap reformatting of long lines;
    ``indent(mapping=2, sequence=4, offset=2)`` keeps the house indentation
    (sequences indented under the key) so untouched sections
    stay byte-identical."""
    yml = YAML()
    yml.preserve_quotes = True
    yml.width = 4096
    yml.indent(mapping=2, sequence=4, offset=2)
    return yml


def _load_doc(path: Path) -> object:
    """Load a spec file: YAML via ruamel round trip (comments stay attached to
    the document), JSON via ``json`` (there are no comments there)."""
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return _roundtrip_yaml().load(path.read_text(encoding="utf-8"))


def load_spec_raw(path: Path) -> dict:
    """Load a spec file as a raw (non-expanded) mapping.

    Unlike :func:`load_spec`, here NO vars/templates are
    expanded, NO relay loops generated and NO validation run —
    the file is only parsed. This is the correct draft source for the
    UI graph editor: ``vars`` placeholders stay literal, ``use:``/``with:``/
    ``insert:`` loops stay un-inlined, and all loop keys (``worktree``,
    ``retry``, ``tier``, …) are preserved."""
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) if yaml else json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(_t("specs.spec_must_be_a_mapping", path=path))
    return data


def spec_hash(path: Path) -> str:
    """SHA-256 of the spec file bytes (optimistic lock: base hash for PUT)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dump_doc(path: Path, doc: dict) -> str:
    """Keep the file's save format: YAML round trip (comments/
    order of the untouched parts) or JSON stays JSON."""
    if path.suffix == ".json":
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    buf = io.StringIO()
    _roundtrip_yaml().dump(doc, buf)
    return buf.getvalue()


# Suffix of the one-generation backup written next to a spec by ``save_spec(..., backup=True)``.
# It does not match ``SPEC_GLOBS``, so a backup is never loaded as a project.
SPEC_BACKUP_SUFFIX = ".bak"


def save_spec(
    name: str, update: SpecUpdate, *, base_hash: str | None = None, dry_run: bool = False, backup: bool = False
) -> dict:
    """Saves a :class:`SpecUpdate` into the project's spec file.

    Flow: load project (unknown → ``KeyError``, broken → ``ValueError``),
    check optimistic lock (``base_hash`` ≠ current file hash →
    :class:`SpecConflict`), load the original file round-trip, apply the update
    (merge loops per loop + ``remove_loops``), check the merged spec via the
    full :func:`load_spec` path (expand + relay + validation) on a
    temp file in the same folder — only then write atomically
    (``os.replace``). On validation errors the file stays untouched.
    Returns the validated, saved spec (with corrected ``_path``).

    ``dry_run``: run every check but leave the file untouched (proposal validation).
    ``backup``: copy the previous file to ``<file>.bak`` before replacing it.
    """
    spec = load_projects().get(name)
    if not spec:
        raise KeyError(_t("specs.unknown_project", name=name))
    if spec.get("_error"):
        raise ValueError(_t("specs.project_has_a_broken_spec", name=name, v2=spec["_error"]))
    path = Path(spec["_path"]).resolve()
    if base_hash is not None and spec_hash(path) != base_hash:
        raise SpecConflict(_t("specs.spec_was_changed_externally_hash"))
    doc = _load_doc(path)
    if not isinstance(doc, dict):
        raise ValueError(_t("specs.spec_must_be_a_mapping", path=path))
    if update.loops is not None:
        # Merge per loop (unknown keys of the existing definition
        # are kept) instead of replacing the whole section.
        loops = doc.setdefault("loops", {})
        for lname, ldef in update.loops.items():
            if isinstance(ldef, dict) and isinstance(loops.get(lname), dict):
                loops[lname].update(ldef)
            else:
                loops[lname] = ldef
    for lname in update.remove_loops:
        doc.get("loops", {}).pop(lname, None)
    for key, value in update.project_fields.items():
        doc[key] = value
    merged = _dump_doc(path, doc)
    # Validation on exactly the bytes that are written (temp file in the
    # same folder: relative paths like prompt_file resolve identically).
    # NamedTemporaryFile (unique name) instead of a fixed <file>.tmp: two
    # parallel PUTs must not share the temp file.
    tmp: Path
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as fd:
        fd.write(merged.encode("utf-8"))
        tmp = Path(fd.name)
    try:
        os.chmod(tmp, os.stat(path).st_mode)  # Rechte der Original-Datei beibehalten
        saved = load_spec(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if dry_run:
        tmp.unlink(missing_ok=True)
        saved["_path"] = str(path)
        saved["name"] = spec["name"]
        return saved
    if backup:
        shutil.copy2(path, path.with_name(path.name + SPEC_BACKUP_SUFFIX))
    os.replace(tmp, path)  # atomic: no half-written spec after a crash
    saved["_path"] = str(path)
    saved["name"] = spec["name"]  # canonical name, not derived from the temp path
    saved["_hash"] = spec_hash(path)  # new hash → basis for the next optimistic lock
    return saved


def load_spec(path: Path, *, check_root: bool = True) -> dict:
    """Load, expand (vars/templates) and validate a spec.

    ``check_root=False`` skips the host-dependent check whether ``root`` exists
    (CI validates specs whose project folder does not exist there)."""
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) if yaml else json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(_t("specs.spec_must_be_a_mapping", path=path))
    data = expand_spec(data, path)
    if not data.get("name"):
        data["name"] = spec_sources.default_name(path)
    if not data.get("root") and path.parent.name in spec_sources.TARGET_SPEC_DIR_NAMES:
        data["root"] = str(path.parent.parent)  # Spec in the target repo: the repo is the project
    if data.get("root"):
        # Resolve `~` and env-var paths once here, so all consumers
        # (engine, gitops, scheduler, server) see the absolute path.
        # expandvars FIRST, then expanduser — `~` in the variable VALUE works.
        # (os.path.expandvars does not know ${VAR:-default}; unset stays literal →
        # the is_dir gate fails with an understandable message.)
        data["root"] = str(Path(os.path.expandvars(str(data["root"]))).expanduser())
    data["_path"] = str(path)
    expand_relay_loops(data)  # `relay:` → generierte Relay-Loops (ADR 0037), danach normal validiert
    _validate_spec(data, check_root=check_root)
    return data


def _validate_approval_when(st: dict, where: str) -> list[str]:
    """`approval_when` (ADR 0017): mapping variable → value | list of values.

    Not combinable with `approval: true` (which always pauses anyway)."""
    cond = st.get("approval_when")
    if cond is None:
        return []
    if st.get("approval"):
        return [_t("specs.approval_and_approval_when_are", where=where)]
    if not isinstance(cond, dict) or not cond:
        return [_t("specs.approval_when_must_be_a", where=where)]
    errs = []
    for var, allowed in cond.items():
        values = allowed if isinstance(allowed, list) else [allowed]
        if not values or not all(isinstance(v, (str, int, bool)) for v in values):
            errs.append(_t("specs.approval_when_must_be_a_2", where=where, var=var))
    return errs


def _worktree_types(steps: list) -> set[str]:
    """Worktree-pflichtige Step-Typen, auch in (verschachtelten) `verify.checks`."""
    found: set[str] = set()
    for st in steps:
        if not isinstance(st, dict):
            continue
        if st.get("type") in step_registry.worktree_types():
            found.add(st["type"])
        checks = st.get("checks")
        if isinstance(checks, list):
            found |= _worktree_types(checks)
    return found


def _validate_worktree(lname: str, ldef: dict, steps: list) -> list[str]:
    """``worktree: false`` (ADR 0020) only for loops without worktree steps."""
    wt = ldef.get("worktree", True)
    if not isinstance(wt, bool):
        return [_t("specs.loop_worktree_must_be_true", lname=lname, wt=wt)]
    if wt:
        return []
    needs = sorted(_worktree_types(steps))
    if needs:
        return [f"loop {lname}: worktree: false, aber Steps brauchen den Worktree ({', '.join(needs)})"]
    return []


def _validate_failure_kind(st: dict, where: str) -> list[str]:
    """`failure_kind` (ADR 0035): only shell steps read it (class for exit codes ≠ 0); value from SPEC_FAILURE_KINDS."""
    if st.get("type") != "shell":
        return [_t("specs.failure_kind_only_applies_to", where=where, v2=st.get("type"))]
    value = st["failure_kind"]
    if not isinstance(value, str) or value not in SPEC_FAILURE_KINDS:
        return [_t("specs.failure_kind_must_be_one", where=where, v2=sorted(SPEC_FAILURE_KINDS), value=value)]
    return []


def _validate_backend(st: dict, key: str, where: str, label: str) -> list[str]:
    """Check a step's backend field ``key`` against the adapter registry (empty = env default)."""
    value = st.get(key)
    known = known_backends()
    if value in (None, "") or value in known:
        return []
    return [_t("specs.unknown_available", where=where, label=label, value=value, v4=", ".join(known))]


def _validate_int_field(st: dict, key: str, where: str, minimum: int) -> list[str]:
    """Optionales int-Feld ``key`` (z. B. ``max_rounds``): int >= ``minimum``, kein Bool."""
    value = st.get(key)
    if value is None:
        return []
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return [_t("specs.must_be_int_is", where=where, key=key, minimum=minimum, value=value)]
    return []


def _validate_pr_review_fix(st: dict, where: str) -> list[str]:
    """pr_review-Fix-Runden: ``fix_variant`` String, ``fix_timeout``/``fix_check_timeout`` int >= 1,
    ``fix_checks`` Liste nicht-leerer Strings."""
    errs = _validate_int_field(st, "fix_timeout", where, 1) + _validate_int_field(st, "fix_check_timeout", where, 1)
    if st.get("fix_variant") is not None and not isinstance(st["fix_variant"], str):
        errs.append(_t("specs.fix_variant_must_be_a", where=where, v2=st["fix_variant"]))
    checks = st.get("fix_checks")
    if checks is not None and (
        not isinstance(checks, list) or not all(isinstance(c, str) and c.strip() for c in checks)
    ):
        errs.append(_t("specs.fix_checks_must_be_a", where=where, checks=checks))
    return errs


def _validate_retry_block(raw: object, where: str) -> list[str]:
    """Validate a ``retry:`` block (ADR 0036) via :meth:`RetryPolicy.parse`.

    ``max: 0`` is not an error (disables retry); broken blocks are reported."""
    from .retry import RetryPolicy

    errs: list[str] = []
    RetryPolicy.parse(raw, where, errs)
    return errs


_printed_retry_warnings: set[str] = set()


def _validate_retry_cron(spec: dict, lname: str, ldef: dict) -> list[str]:
    """Cron interval validation (ADR 0036, decision 2) for a loop.

    Check the effective policy (loop ``retry`` replaces the project block) against the
    cron interval: backoff value ≥ interval → error; the whole chain
    longer than the interval → warning (no spec error). Specs are reloaded on
    every scheduler tick and request — each warning is therefore printed only
    once per process."""
    from .retry_chains import policy_for, validate_retry_cron

    policy = policy_for(spec, lname)
    errs, warns = validate_retry_cron(spec, lname, ldef, policy)
    for w in warns:
        msg = f"{spec.get('name', '?')}: {w}"
        if msg not in _printed_retry_warnings:
            _printed_retry_warnings.add(msg)
            print(f"[fabrikant] {msg}")
    return errs


def _validate_step_shell(st: dict, where: str, spec: dict) -> list[str]:
    if not st.get("cmd"):
        return [_t("specs.shell_without_cmd", where=where)]
    return []


def _validate_step_http(st: dict, where: str, spec: dict) -> list[str]:
    if not st.get("url"):
        return [_t("specs.http_without_url", where=where)]
    return []


def _validate_step_llm(st: dict, where: str, spec: dict) -> list[str]:
    if not st.get("prompt"):
        return [_t("specs.llm_without_prompt", where=where)]
    return billing.validate(st, where)


def _validate_step_state(st: dict, where: str, spec: dict) -> list[str]:
    errs = []
    if st.get("action") not in ("get", "set", "add"):
        errs.append(_t("specs.state_action_must_be_get", where=where, v2=st.get("action")))
    if not st.get("key"):
        errs.append(_t("specs.state_without_key", where=where))
    return errs


def _validate_step_verify(st: dict, where: str, spec: dict) -> list[str]:
    checks = st.get("checks")
    if not isinstance(checks, list):
        return [_t("specs.verify_needs_checks_as_a", where=where)]
    errs = []
    for j, chk in enumerate(checks):
        cwhere = f"{where}: verify-Check {j}"
        t = chk.get("type") if isinstance(chk, dict) else None
        if t not in step_registry.known_types():
            errs.append(_t("specs.needs_type_in_is", cwhere=cwhere, v2=list(step_registry.known_types()), t=t))
        # extra_context also applies to checks (effective_step, ADR 0008): only for LLM step types.
        if isinstance(chk, dict) and extra_context.KEY in chk:
            errs += extra_context.validate(chk[extra_context.KEY], cwhere, t)
    return errs


def _validate_step_agent(st: dict, where: str, spec: dict) -> list[str]:
    errs = []
    if not (st.get("prompt") or st.get("prompt_file")):
        errs.append(_t("specs.agent_without_prompt_prompt_file", where=where))
    errs += _validate_backend(st, "backend", where, "agent backend")
    errs += billing.validate(st, where)
    if st.get("variant") is not None and not isinstance(st["variant"], str):
        errs.append(_t("specs.agent_variant_must_be_a", where=where, v2=st["variant"]))
    gid = st.get("github_identity")
    if gid is not None and gid not in GH_TOKEN_ENV:
        errs.append(_t("specs.github_identity_must_be_one", where=where, v2=sorted(GH_TOKEN_ENV), gid=gid))
    if st.get("workdir") is not None and not isinstance(st["workdir"], str):
        errs.append(_t("specs.agent_workdir_must_be_a", where=where))
    if st.get("timeout") is not None:
        try:
            t = int(st["timeout"])
            if t <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errs.append(_t("specs.agent_timeout_must_be_a", where=where))
    return errs


def _validate_step_review(st: dict, where: str, spec: dict) -> list[str]:
    errs = []
    if not st.get("model"):
        errs.append(_t("specs.review_without_model_required_builder", where=where))
    errs += _validate_backend(st, "backend", where, "review backend")
    return errs


def _validate_step_alert_route(st: dict, where: str, spec: dict) -> list[str]:
    return alerts.validate_route_step(st, spec, where)


def _validate_step_pr_review(st: dict, where: str, spec: dict) -> list[str]:
    errs = []
    if not st.get("repo"):
        errs.append(_t("specs.pr_review_without_repo_owner", where=where))
    if not st.get("model"):
        errs.append(_t("specs.pr_review_without_model_required", where=where))
    errs += _validate_backend(st, "backend", where, "pr_review backend")
    errs += _validate_backend(st, "fix_backend", where, "pr_review fix_backend")
    # max_rounds 0 = Review-only (keine Fix-Runden); max_findings >= 1 (ADR 0039).
    errs += _validate_int_field(st, "max_rounds", where, 0)
    errs += _validate_int_field(st, "max_findings", where, 1)
    errs += _validate_pr_review_fix(st, where)
    return errs


# Step type → validation helper (error list). New step types attach
# an entry + helper here — `_validate_spec` itself does not grow any further.
# `spec` is the whole project def (for step types that need context,
# e.g. `alert_route` checks the route chain across all loops).
_STEP_VALIDATORS: dict[str, Callable[[dict, str, dict], list[str]]] = {
    "shell": _validate_step_shell,
    "http": _validate_step_http,
    "llm": _validate_step_llm,
    "state": _validate_step_state,
    "verify": _validate_step_verify,
    "agent": _validate_step_agent,
    "review": _validate_step_review,
    "pr_review": _validate_step_pr_review,
    alerts.STEP_TYPE: _validate_step_alert_route,
}


def _validate_step_type(stype: object, where: str) -> list[str]:
    """The step type must be built in or a registered plugin (ADR 0069); the schema only checks `string`."""
    if not isinstance(stype, str) or stype in step_registry.known_types():
        return []
    return [_t("specs.needs_type_in_is", cwhere=where, v2=list(step_registry.known_types()), t=stype)]


def _validate_plugin_step(st: dict, where: str) -> list[str]:
    """Validate a plugin step against the JSON-schema fragment its plugin registered (if any)."""
    stype = st.get("type")
    plugin = step_registry.get_step(stype) if isinstance(stype, str) else None
    if plugin is None or plugin.schema is None:
        return []
    validator = Draft202012Validator(plugin.schema)
    return [
        f"{where}: {'/'.join(str(p) for p in e.absolute_path) or '<step>'}: {e.message}"
        for e in sorted(validator.iter_errors(st), key=lambda e: list(e.absolute_path))
    ]


def _validate_spec(spec: dict, *, check_root: bool = True) -> None:
    errs = []
    # Structural schema (issue #131): types/enums/required fields from
    # src/fabrikant/fabrikant-spec.schema.json; semantic checks follow below.
    errs += specschema.validate_spec(spec)
    # Lock key "project::loop" (ADR 0020) stays unique only if the separator is absent from names.

    for what, value in [("name", spec.get("name"))] + [("loop", ln) for ln in (spec.get("loops") or {})]:
        if isinstance(value, str) and LOCK_KEY_SEP in value:
            errs.append(_t("specs.must_not_contain_lock_key", what=what, value=value, v3=LOCK_KEY_SEP))
    if not spec.get("root") or (check_root and not Path(spec["root"]).is_dir()):
        errs.append(_t("specs.root_missing_or_does_not", v1=spec.get("root")))
    gef = spec.get(SPEC_ENV_FILE_KEY)
    # The schema checks the type (string); only the empty/absolute check here, so errors are not reported twice.
    if isinstance(gef, str) and not gef.strip():
        errs.append(_t("specs.must_be_a_path_string", v1=SPEC_ENV_FILE_KEY, gef=gef))
    elif isinstance(gef, str) and not Path(os.path.expandvars(gef)).expanduser().is_absolute():
        errs.append(_t("specs.must_be_an_absolute_path", v1=SPEC_ENV_FILE_KEY, gef=gef))
    hosting_name = spec.get(HOSTING_KEY)
    # The schema type-checks non-strings; only here: a string must be a registered provider.
    if isinstance(hosting_name, str) and hosting_name.strip().lower() not in registered_providers():
        errs.append(
            _t("specs.must_be_registered_provider", v1=HOSTING_KEY, v2=registered_providers(), name=hosting_name)
        )
    if extra_context.KEY in spec:
        errs += extra_context.validate(spec[extra_context.KEY], "projekt", allow_false=False)
    mp = spec.get("max_parallel")
    if mp is not None and (isinstance(mp, bool) or not isinstance(mp, int) or mp < 1):
        errs.append(_t("specs.max_parallel_must_be_int", mp=mp))
    if spec.get("retry") is not None:
        errs += _validate_retry_block(spec["retry"], "retry")
    loops = spec.get("loops")
    if not isinstance(loops, dict) or not loops:
        errs.append(_t("specs.loops_at_least_one_loop"))
    else:
        for lname, ldef in loops.items():
            if not isinstance(ldef, dict):
                errs.append(_t("specs.loop_mapping_expected", lname=lname))
                continue
            steps = ldef.get("steps")
            if not isinstance(steps, list) or not steps:
                errs.append(_t("specs.loop_steps_missing", lname=lname))
                continue
            for ti, trig in enumerate(ldef.get("triggers") or []):
                if isinstance(trig, dict) and trig.get("type") == "on_webhook":
                    errs += webhook.validate_trigger(trig, f"loop {lname} trigger {ti}")
            for i, trig in enumerate(ldef.get("triggers") or []):
                if not isinstance(trig, dict):
                    errs.append(_t("specs.loop_trigger_mapping_expected", lname=lname, i=i))
                    continue
                errs.extend(_validate_trigger(lname, i, trig, spec))
            errs += _validate_worktree(lname, ldef, steps)
            if "retry" in ldef:
                errs += _validate_retry_block(ldef["retry"], f"loop {lname}.retry")
            errs += _validate_retry_cron(spec, lname, ldef)
            for i, st in enumerate(steps):
                if isinstance(st, dict):
                    where = f"loop {lname} step {i}"
                    errs += _validate_approval_when(st, where)
                    if extra_context.KEY in st:
                        errs += extra_context.validate(st[extra_context.KEY], where, st.get("type"))
                    if "approval_reason" in st and not isinstance(st["approval_reason"], str):
                        errs.append(_t("specs.approval_reason_must_be_a", where=where))
                    if "failure_kind" in st:
                        errs += _validate_failure_kind(st, where)
                    stype = st.get("type")
                    errs += _validate_step_type(stype, where)
                    validator = _STEP_VALIDATORS.get(stype) if isinstance(stype, str) else None
                    if validator is not None:
                        errs += validator(st, where, spec)
                    errs += _validate_plugin_step(st, where)
    if errs:
        raise ValueError(_t("specs.spec_invalid") + "\n  ".join(errs))


def _as_number(value: object) -> float:
    """Number or number string (from `vars`) as float; bool/other → NaN."""
    if isinstance(value, bool):
        return math.nan
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.nan


def _validate_poll(value: object, where: str) -> list[str]:
    """Optional `poll` interval: finite number >= 0 (also as a number string from `vars`), no bool.

    The scheduler computes with `float(poll)`; invalid values would otherwise only show up in the
    tick as ValueError or let a NaN interval never/always fire."""
    if value is None:
        return []
    seconds = _as_number(value)
    if not math.isfinite(seconds) or seconds < 0:
        return [_t("specs.poll_must_be_a_finite", where=where, value=value)]
    return []


def _validate_positive_int(trig: dict, key: str, where: str) -> list[str]:
    """Optional trigger field `key`: integer >= 1 (also as a number string from `vars`), no bool."""
    value = trig.get(key)
    if value is None:
        return []
    number = _as_number(value)
    if not math.isfinite(number) or number < 1 or not number.is_integer():
        return [_t("specs.must_be_an_integer_1", where=where, key=key, value=value)]
    return []


def _validate_trigger(lname: str, i: int, trig: dict, spec: dict) -> list[str]:
    """Validates a trigger def; returns a list of error messages.

    `on_issue`/`on_pr` need a repo (trigger or spec level) — without a
    repo the trigger would silently never fire (config error instead of silent no-op).
    """
    errs = []
    ttype = trig.get("type")
    if ttype not in config.TRIGGER_TYPES:
        return errs  # unknown type: reported by the schema (enum), no duplicate here

    if ttype == "on_slack":
        errs += slack.validate_trigger(trig, f"loop {lname} trigger {i}")
        for key in ("max_pages", "max_alert_chars"):
            errs += _validate_positive_int(trig, key, f"loop {lname} trigger {i}")
    if ttype in config.POLL_TRIGGER_TYPES:
        errs += _validate_poll(trig.get("poll"), f"loop {lname} trigger {i}")
    if ttype in ("on_issue", "on_pr") and not (trig.get("repo") or spec.get("github")):
        errs.append(_t("specs.loop_trigger_without_repo_trigger", lname=lname, i=i, ttype=ttype))
    if ttype == alerts.TRIGGER_TYPE:
        errs += alerts.validate_trigger(trig, f"loop {lname} trigger {i}")
    if trig.get("skip_blocked") and not (ttype == "on_issue" and trig.get("reconcile")):
        # Event mode fires each issue only once — a blocked issue would be
        # lost instead of starting after the blocker is closed (ADR 0031).
        errs.append(_t("specs.loop_trigger_skip_blocked_only", lname=lname, i=i))
    exclude = trig.get("exclude_labels")
    if exclude is not None and (
        ttype not in ("on_issue", "on_pr")
        or not isinstance(exclude, list)
        or not all(isinstance(x, str) and x for x in exclude)
    ):
        errs.append(_t("specs.loop_trigger_exclude_labels", lname=lname, i=i, exclude=exclude))
    on_start = trig.get("on_start")
    if on_start is not None and (ttype != "cron" or not isinstance(on_start, bool)):
        errs.append(_t("specs.loop_trigger_on_start_only", lname=lname, i=i, on_start=on_start))
    if ttype == "on_pr":
        states = trig.get("states")
        if states is not None and (
            not isinstance(states, list) or not states or any(s not in config.PR_STATES for s in states)
        ):
            errs.append(
                _t("specs.loop_trigger_on_pr_states", lname=lname, i=i, v3=list(config.PR_STATES), states=states)
            )
        rec = trig.get("reconcile")
        if rec is not None and not isinstance(rec, bool) and rec != config.RECONCILE_PER_HEAD:
            errs.append(
                _t("specs.loop_trigger_on_pr_reconcile", lname=lname, i=i, v3=config.RECONCILE_PER_HEAD, rec=rec)
            )
    return errs


def collect_projects(files: list[Path] | None = None, *, check_root: bool = True) -> tuple[dict[str, dict], list[str]]:
    """Specs of all sources (spec_sources.spec_files) + name conflicts.

    On equal names the source with the higher priority wins; every shadowed
    spec produces a conflict message (``fabrikant validate`` then fails).
    ``files`` replaces the sources (e.g. CI: only the specs in the repo); ``check_root`` like
    :func:`load_spec`.
    """
    projects: dict[str, dict] = {}
    conflicts: list[str] = []
    for f in spec_sources.spec_files() if files is None else files:
        try:
            spec = load_spec(f, check_root=check_root)
        except Exception as e:
            spec = {"name": spec_sources.default_name(f), "_error": str(e)}
        name = spec["name"]
        if name in projects:
            conflicts.append(
                _t("specs.project_defined_more_than_once", name=name, v2=projects[name].get("_path") or "?", f=f)
            )
            continue
        spec.setdefault("_path", str(f))
        projects[name] = spec
    return projects, conflicts


def load_projects() -> dict[str, dict]:
    """Loads all specs; broken specs are marked with _error.

    Branch overrides (state/branch-overrides.json, ADR 0004) are laid over them
    at the end, so server/engine/CLI see the branch chosen at runtime.
    """
    return branches.apply_overrides(collect_projects()[0])
