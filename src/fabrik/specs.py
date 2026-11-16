"""Spec-Loading, -Validierung & -Speicherung (Quellen: spec_sources, vars/Templates: spec_expand; ADR 0047).

Speichern (:func:`save_spec`) nutzt ``ruamel.yaml`` im Round-Trip-Modus, damit
Kommentare und Reihenfolge der unangetasteten Datei-Teile erhalten bleiben."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML

try:
    import yaml
except ImportError:
    yaml = None

from . import alerts, branches, config, slack, spec_sources, webhook
from .backends import known_backends
from .failures import SPEC_FAILURE_KINDS
from .gitops import COMMIT_VIA_MODES, GH_TOKEN_ENV, SPEC_COMMIT_VIA_KEY, SPEC_ENV_FILE_KEY
from .relay.config import expand_loops as expand_relay_loops
from .runs import LOCK_KEY_SEP
from .spec_expand import expand_spec
from .steps import extra_context


class SpecConflict(ValueError):
    """Optimistischer Lock-Verstoß: die Spec wurde seit dem Laden des Drafts
    extern geändert (``base_hash`` ≠ aktueller Datei-Hash). Der Client soll den
    Draft neu laden statt die fremden Änderungen zu überschreiben (→ HTTP 409)."""


def spec_path(name: str, projects: dict | None = None) -> Path:
    """Return the source path for a loaded project, across all spec sources.

    Callers that already hold `load_projects()` pass it as `projects` (avoids a reload)."""
    spec = (load_projects() if projects is None else projects).get(name)
    if spec and spec.get("_path"):
        return Path(spec["_path"])
    return config.PROJECTS_DIR / f"{name}.fabrik.yaml"


# Top-Level-Felder, die per PUT /api/projects/<name>/spec editierbar sind.
# Whitelist statt Freifahrtschein: `name`/`root`/`loops`/`vars` & Co. bleiben
# unangetastet (Name ist an den Dateinamen gekoppelt, `vars` an die Expansion).
SPEC_EDITABLE_FIELDS = ("description", "max_parallel")


@dataclass
class SpecUpdate:
    """Speicher-Update für ein Projekt (``PUT /api/projects/<name>/spec``).

    ``loops`` enthält die zu speichernden Loops (None = unverändert) — sie werden
    pro Loop gemergt (unbekannte Schlüssel der bestehenden Definition bleiben
    erhalten), nicht die ganze Sektion ersetzt. ``remove_loops`` nennt Loops, die
    gelöscht werden. ``project_fields`` aktualisiert einzelne Top-Level-Felder aus
    :data:`SPEC_EDITABLE_FIELDS`."""

    loops: dict | None = None
    remove_loops: list[str] = field(default_factory=list)
    project_fields: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict) -> SpecUpdate:
        """JSON-Request-Body → :class:`SpecUpdate`; strukturell ungültig → ``ValueError``.

        Unbekannte Felder werden abgelehnt (Typo-Schutz), damit ein
        ``max_paralell``-Feld nicht stumm ignoriert wird."""
        loops = payload.get("loops")
        if loops is not None and not isinstance(loops, dict):
            raise ValueError("loops muss ein Objekt (Mapping Loop-Name → Definition) sein")
        remove = payload.get("remove_loops") or []
        if not isinstance(remove, list) or not all(isinstance(n, str) for n in remove):
            raise ValueError("remove_loops muss eine Liste von Loop-Namen sein")
        fields: dict[str, object] = {}
        for key in SPEC_EDITABLE_FIELDS:
            if key not in payload:
                continue
            value = payload[key]
            if key == "description" and not isinstance(value, str):
                raise ValueError("description muss ein String sein")
            if key == "max_parallel" and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError("max_parallel muss eine ganze Zahl sein")
            fields[key] = value
        unknown = sorted(set(payload) - {"loops", "remove_loops", *SPEC_EDITABLE_FIELDS})
        if unknown:
            raise ValueError(
                f"unbekannte Felder: {', '.join(unknown)} (erlaubt: loops, remove_loops, "
                f"{', '.join(SPEC_EDITABLE_FIELDS)})"
            )
        return cls(loops=loops, remove_loops=list(remove), project_fields=fields)


def _roundtrip_yaml() -> YAML:
    """ruamel-YAML-Instanz im Round-Trip-Modus (Kommentare, Reihenfolge und
    Quotes der unangetasteten Datei-Teile bleiben erhalten).

    ``width=4096`` verhindert das Umbruch-Neuformatieren langer Zeilen;
    ``indent(mapping=2, sequence=4, offset=2)`` hält die Haus-Indentation
    (Sequenzen eingerückt unter dem Key), damit unangetastete Sektionen
    byte-identisch bleiben."""
    yml = YAML()
    yml.preserve_quotes = True
    yml.width = 4096
    yml.indent(mapping=2, sequence=4, offset=2)
    return yml


def _load_doc(path: Path) -> object:
    """Spec-Datei laden: YAML per ruamel Round-Trip (Kommentare bleiben am
    Dokument hängen), JSON per ``json`` (dort gibt es keine Kommentare)."""
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return _roundtrip_yaml().load(path.read_text(encoding="utf-8"))


def load_spec_raw(path: Path) -> dict:
    """Spec-Datei als rohes (nicht expandiertes) Mapping laden.

    Im Gegensatz zu :func:`load_spec` werden hier KEINE vars/Templates
    expandiert, KEINE Relay-Loops generiert und KEINE Validierung gefahren —
    die Datei wird nur geparst. Das ist die korrekte Draft-Quelle für den
    UI-Graph-Editor: ``vars``-Platzhalter bleiben wörtlich, ``use:``/``with:``/
    ``insert:``-Loops bleiben un-inlined, und alle Loop-Schlüssel (``worktree``,
    ``retry``, ``tier``, …) bleiben erhalten."""
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) if yaml else json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Spec muss ein Mapping sein: {path}")
    return data


def spec_hash(path: Path) -> str:
    """SHA-256 der Spec-Datei-Bytes (Optimistischer Lock: Basis-Hash für PUT)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dump_doc(path: Path, doc: dict) -> str:
    """Speich-Format der Datei beibehalten: YAML Round-Trip (Kommentare/
    Reihenfolge der unangetasteten Teile) bzw. JSON bleibt JSON."""
    if path.suffix == ".json":
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    buf = io.StringIO()
    _roundtrip_yaml().dump(doc, buf)
    return buf.getvalue()


def save_spec(name: str, update: SpecUpdate, *, base_hash: str | None = None) -> dict:
    """Speichert ein :class:`SpecUpdate` in der Spec-Datei des Projekts.

    Ablauf: Projekt laden (unbekannt → ``KeyError``, defekt → ``ValueError``),
    Optimistischer Lock prüfen (``base_hash`` ≠ aktueller Datei-Hash →
    :class:`SpecConflict`), Original-Datei round-trip laden, Update anwenden
    (Loops pro Loop mergen + ``remove_loops``), die gemergte Spec über den
    vollen :func:`load_spec`-Pfad (Expand + Relay + Validierung) auf einer
    Temp-Datei im selben Ordner prüfen — erst dann atomar schreiben
    (``os.replace``). Bei Validierungsfehlern bleibt die Datei unangetastet.
    Liefert die validierte, gespeicherte Spec (mit korrigiertem ``_path``).
    """
    spec = load_projects().get(name)
    if not spec:
        raise KeyError(f"unbekanntes Projekt: {name}")
    if spec.get("_error"):
        raise ValueError(f"Projekt {name} hat eine defekte Spec: {spec['_error']}")
    path = Path(spec["_path"]).resolve()
    if base_hash is not None and spec_hash(path) != base_hash:
        raise SpecConflict("Spec wurde extern geändert (Hash-Mismatch) — Draft neu laden")
    doc = _load_doc(path)
    if not isinstance(doc, dict):
        raise ValueError(f"Spec muss ein Mapping sein: {path}")
    if update.loops is not None:
        # Pro Loop mergen (unbekannte Schlüssel der bestehenden Definition
        # bleiben erhalten) statt die ganze Sektion zu ersetzen.
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
    # Validierung auf exakt den Bytes, die geschrieben werden (Temp-Datei im
    # selben Ordner: relative Pfade wie prompt_file lösen sich identisch auf).
    # NamedTemporaryFile (eindeutiger Name) statt fixem <file>.tmp: zwei
    # parallele PUTs dürfen sich nicht die Temp-Datei teilen.
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
    os.replace(tmp, path)  # atomar: kein halb geschriebenes Spec nach Crash
    saved["_path"] = str(path)
    saved["name"] = spec["name"]  # kanonischer Name, nicht aus dem Temp-Pfad abgeleitet
    saved["_hash"] = spec_hash(path)  # neuer Hash → Basis für den nächsten Optimistischen Lock
    return saved


def load_spec(path: Path, *, check_root: bool = True) -> dict:
    """Spec laden, expandieren (vars/Templates) und validieren.

    ``check_root=False`` überspringt die host-abhängige Prüfung, ob ``root`` existiert
    (CI validiert Specs, deren Projekt-Ordner es dort nicht gibt)."""
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) if yaml else json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Spec muss ein Mapping sein: {path}")
    data = expand_spec(data, path)
    if not data.get("name"):
        data["name"] = spec_sources.default_name(path)
    if not data.get("root") and path.parent.name == config.TARGET_SPEC_RELPATH.parent.name:
        data["root"] = str(path.parent.parent)  # Spec im Zielrepo: das Repo ist das Projekt
    if data.get("root"):
        # `~`- und Env-Var-Pfade hier einmalig auflösen, damit alle Konsumenten
        # (engine, gitops, scheduler, server) den absoluten Pfad sehen.
        # expandvars ERST, dann expanduser — `~` im VariablENWERT funktioniert.
        # (os.path.expandvars kennt kein ${VAR:-default}; unset bleibt wörtlich →
        # is_dir-Gate failt mit verständlicher Meldung.)
        data["root"] = str(Path(os.path.expandvars(str(data["root"]))).expanduser())
    data["_path"] = str(path)
    expand_relay_loops(data)  # `relay:` → generierte Relay-Loops (ADR 0037), danach normal validiert
    _validate_spec(data, check_root=check_root)
    return data


def _validate_approval_when(st: dict, where: str) -> list[str]:
    """`approval_when` (ADR 0017): Mapping Variable → Wert | Liste von Werten.

    Nicht kombinierbar mit `approval: true` (das pausiert ohnehin immer)."""
    cond = st.get("approval_when")
    if cond is None:
        return []
    if st.get("approval"):
        return [f"{where}: approval und approval_when schließen sich aus"]
    if not isinstance(cond, dict) or not cond:
        return [f"{where}: approval_when muss ein nicht-leeres Mapping sein (Variable → Wert/Werte)"]
    errs = []
    for var, allowed in cond.items():
        values = allowed if isinstance(allowed, list) else [allowed]
        if not values or not all(isinstance(v, (str, int, bool)) for v in values):
            errs.append(f"{where}: approval_when.{var} muss Wert oder nicht-leere Werte-Liste sein")
    return errs


def _worktree_types(steps: list) -> set[str]:
    """Worktree-pflichtige Step-Typen, auch in (verschachtelten) `verify.checks`."""
    found: set[str] = set()
    for st in steps:
        if not isinstance(st, dict):
            continue
        if st.get("type") in config.WORKTREE_STEP_TYPES:
            found.add(st["type"])
        checks = st.get("checks")
        if isinstance(checks, list):
            found |= _worktree_types(checks)
    return found


def _validate_worktree(lname: str, ldef: dict, steps: list) -> list[str]:
    """``worktree: false`` (ADR 0020) nur für Loops ohne Worktree-Steps."""
    wt = ldef.get("worktree", True)
    if not isinstance(wt, bool):
        return [f"loop {lname}: worktree muss true/false sein (ist {wt!r})"]
    if wt:
        return []
    needs = sorted(_worktree_types(steps))
    if needs:
        return [f"loop {lname}: worktree: false, aber Steps brauchen den Worktree ({', '.join(needs)})"]
    return []


def _validate_failure_kind(st: dict, where: str) -> list[str]:
    """`failure_kind` (ADR 0035): nur shell-Steps lesen es (Klasse für Exit-Codes ≠ 0); Wert aus SPEC_FAILURE_KINDS."""
    if st.get("type") != "shell":
        return [f"{where}: failure_kind gilt nur für shell-Steps (ist {st.get('type')!r})"]
    value = st["failure_kind"]
    if not isinstance(value, str) or value not in SPEC_FAILURE_KINDS:
        return [f"{where}: failure_kind muss einer von {sorted(SPEC_FAILURE_KINDS)} sein (ist {value!r})"]
    return []


def _validate_backend(st: dict, key: str, where: str, label: str) -> list[str]:
    """Backend-Feld ``key`` eines Steps gegen die Adapter-Registry prüfen (leer = Env-Default)."""
    value = st.get(key)
    known = known_backends()
    if value in (None, "") or value in known:
        return []
    return [f"{where}: {label} '{value}' unbekannt (verfügbar: {', '.join(known)})"]


def _validate_int_field(st: dict, key: str, where: str, minimum: int) -> list[str]:
    """Optionales int-Feld ``key`` (z. B. ``max_rounds``): int >= ``minimum``, kein Bool."""
    value = st.get(key)
    if value is None:
        return []
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return [f"{where}: {key} muss int >= {minimum} sein (ist {value!r})"]
    return []


def _validate_pr_review_fix(st: dict, where: str) -> list[str]:
    """pr_review-Fix-Runden: ``fix_variant`` String, ``fix_timeout``/``fix_check_timeout`` int >= 1,
    ``fix_checks`` Liste nicht-leerer Strings."""
    errs = _validate_int_field(st, "fix_timeout", where, 1) + _validate_int_field(st, "fix_check_timeout", where, 1)
    if st.get("fix_variant") is not None and not isinstance(st["fix_variant"], str):
        errs.append(f"{where}: fix_variant muss String sein (ist {st['fix_variant']!r})")
    checks = st.get("fix_checks")
    if checks is not None and (
        not isinstance(checks, list) or not all(isinstance(c, str) and c.strip() for c in checks)
    ):
        errs.append(f"{where}: fix_checks muss Liste nicht-leerer Shell-Kommandos sein (ist {checks!r})")
    return errs


def _validate_retry_block(raw: object, where: str) -> list[str]:
    """``retry:``-Block (ADR 0036) per :meth:`RetryPolicy.parse` validieren.

    ``max: 0`` ist kein Fehler (schaltet Retry ab); defekte Blöcke werden gemeldet."""
    from .retry import RetryPolicy

    errs: list[str] = []
    RetryPolicy.parse(raw, where, errs)
    return errs


_printed_retry_warnings: set[str] = set()


def _validate_retry_cron(spec: dict, lname: str, ldef: dict) -> list[str]:
    """Cron-Intervall-Validierung (ADR 0036, Entscheidung 2) für einen Loop.

    Wirksame Policy (Loop-``retry`` ersetzt den Projekt-Block) gegen das
    Cron-Intervall prüfen: Backoff-Wert ≥ Intervall → Fehler; die ganze Kette
    länger als das Intervall → Warnung (kein Spec-Fehler). Specs werden bei
    jedem Scheduler-Tick und Request neu geladen — jede Warnung wird daher nur
    einmal pro Prozess gedruckt."""
    from .retry_chains import policy_for, validate_retry_cron

    policy = policy_for(spec, lname)
    errs, warns = validate_retry_cron(spec, lname, ldef, policy)
    for w in warns:
        msg = f"{spec.get('name', '?')}: {w}"
        if msg not in _printed_retry_warnings:
            _printed_retry_warnings.add(msg)
            print(f"[fabrik] {msg}")
    return errs


def _validate_step_shell(st: dict, where: str, spec: dict) -> list[str]:
    if not st.get("cmd"):
        return [f"{where}: shell ohne cmd"]
    return []


def _validate_step_http(st: dict, where: str, spec: dict) -> list[str]:
    if not st.get("url"):
        return [f"{where}: http ohne url"]
    return []


def _validate_step_llm(st: dict, where: str, spec: dict) -> list[str]:
    if not st.get("prompt"):
        return [f"{where}: llm ohne prompt"]
    return []


def _validate_step_state(st: dict, where: str, spec: dict) -> list[str]:
    errs = []
    if st.get("action") not in ("get", "set", "add"):
        errs.append(f"{where}: state action muss get|set|add sein (ist {st.get('action')!r})")
    if not st.get("key"):
        errs.append(f"{where}: state ohne key")
    return errs


def _validate_step_verify(st: dict, where: str, spec: dict) -> list[str]:
    checks = st.get("checks")
    if not isinstance(checks, list):
        return [f"{where}: verify braucht 'checks' als Liste"]
    errs = []
    for j, chk in enumerate(checks):
        cwhere = f"{where}: verify-Check {j}"
        t = chk.get("type") if isinstance(chk, dict) else None
        if t not in config.STEP_TYPES:
            errs.append(f"{cwhere} braucht 'type' in {list(config.STEP_TYPES)} (ist {t!r})")
        # extra_context wirkt auch auf Checks (effective_step, ADR 0008): nur bei LLM-Step-Typen.
        if isinstance(chk, dict) and extra_context.KEY in chk:
            errs += extra_context.validate(chk[extra_context.KEY], cwhere, t)
    return errs


def _validate_step_agent(st: dict, where: str, spec: dict) -> list[str]:
    errs = []
    if not (st.get("prompt") or st.get("prompt_file")):
        errs.append(f"{where}: agent ohne prompt/prompt_file")
    errs += _validate_backend(st, "backend", where, "agent backend")
    if st.get("variant") is not None and not isinstance(st["variant"], str):
        errs.append(f"{where}: agent variant muss String sein (ist {st['variant']!r})")
    gid = st.get("github_identity")
    if gid is not None and gid not in GH_TOKEN_ENV:
        errs.append(f"{where}: github_identity muss einer von {sorted(GH_TOKEN_ENV)} sein (ist {gid!r})")
    if st.get("workdir") is not None and not isinstance(st["workdir"], str):
        errs.append(f"{where}: agent workdir muss String sein")
    if st.get("timeout") is not None:
        try:
            t = int(st["timeout"])
            if t <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errs.append(f"{where}: agent timeout muss positive Zahl sein")
    return errs


def _validate_step_review(st: dict, where: str, spec: dict) -> list[str]:
    errs = []
    if not st.get("model"):
        errs.append(f"{where}: review ohne model (Pflicht, ≠ Builder-Modell)")
    errs += _validate_backend(st, "backend", where, "review backend")
    return errs


def _validate_step_alert_route(st: dict, where: str, spec: dict) -> list[str]:
    return alerts.validate_route_step(st, spec, where)


def _validate_step_pr_review(st: dict, where: str, spec: dict) -> list[str]:
    errs = []
    if not st.get("repo"):
        errs.append(f"{where}: pr_review ohne repo (owner/name)")
    if not st.get("model"):
        errs.append(f"{where}: pr_review ohne model (Pflicht, ≠ Builder-Modell)")
    errs += _validate_backend(st, "backend", where, "pr_review backend")
    errs += _validate_backend(st, "fix_backend", where, "pr_review fix_backend")
    # max_rounds 0 = Review-only (keine Fix-Runden); max_findings >= 1 (ADR 0039).
    errs += _validate_int_field(st, "max_rounds", where, 0)
    errs += _validate_int_field(st, "max_findings", where, 1)
    errs += _validate_pr_review_fix(st, where)
    return errs


# Step-Type → Validierungs-Helfer (Fehlerliste). Neue Step-Types hängen hier
# einen Eintrag + Helfer an — `_validate_spec` selbst wächst nicht weiter.
# `spec` ist die ganze Projekt-Def (für Step-Types, die Kontext brauchen,
# z. B. `alert_route` prüft die Route-Kette über alle Loops).
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


def _validate_spec(spec: dict, *, check_root: bool = True) -> None:
    errs = []
    # Lock-Schlüssel "projekt::loop" (ADR 0020) bleibt nur eindeutig, wenn der Separator in Namen fehlt.
    for what, value in [("name", spec.get("name"))] + [("loop", ln) for ln in (spec.get("loops") or {})]:
        if isinstance(value, str) and LOCK_KEY_SEP in value:
            errs.append(f"{what} {value!r}: darf {LOCK_KEY_SEP!r} nicht enthalten (Lock-Schlüssel)")
    if not spec.get("root") or (check_root and not Path(spec["root"]).is_dir()):
        errs.append(f"root fehlt oder existiert nicht: {spec.get('root')!r}")
    gef = spec.get(SPEC_ENV_FILE_KEY)
    if gef is not None and (not isinstance(gef, str) or not gef.strip()):
        errs.append(f"{SPEC_ENV_FILE_KEY} muss ein Pfad (String) sein (ist {gef!r})")
    elif gef is not None and not Path(os.path.expandvars(gef)).expanduser().is_absolute():
        errs.append(f"{SPEC_ENV_FILE_KEY} muss ein absoluter Pfad sein (ist {gef!r})")
    cvia = spec.get(SPEC_COMMIT_VIA_KEY)
    if cvia is not None and cvia not in COMMIT_VIA_MODES:
        errs.append(f"{SPEC_COMMIT_VIA_KEY} muss einer von {COMMIT_VIA_MODES} sein (ist {cvia!r})")
    if extra_context.KEY in spec:
        errs += extra_context.validate(spec[extra_context.KEY], "projekt", allow_false=False)
    mp = spec.get("max_parallel")
    if mp is not None and (isinstance(mp, bool) or not isinstance(mp, int) or mp < 1):
        errs.append(f"max_parallel muss int >= 1 sein (ist {mp!r})")
    if spec.get("retry") is not None:
        errs += _validate_retry_block(spec["retry"], "retry")
    loops = spec.get("loops")
    if not isinstance(loops, dict) or not loops:
        errs.append("loops: mindestens ein Loop nötig")
    else:
        for lname, ldef in loops.items():
            if not isinstance(ldef, dict):
                errs.append(f"loop {lname}: Mapping erwartet")
                continue
            steps = ldef.get("steps")
            if not isinstance(steps, list) or not steps:
                errs.append(f"loop {lname}: steps fehlt")
                continue
            for ti, trig in enumerate(ldef.get("triggers") or []):
                if isinstance(trig, dict) and trig.get("type") == "on_webhook":
                    errs += webhook.validate_trigger(trig, f"loop {lname} trigger {ti}")
            tier = ldef.get("tier")
            if tier is not None and (not isinstance(tier, int) or not 1 <= tier <= 4):
                errs.append(f"loop {lname}: tier muss int 1-4 sein (ist {tier!r})")
            for i, trig in enumerate(ldef.get("triggers") or []):
                if not isinstance(trig, dict):
                    errs.append(f"loop {lname} trigger {i}: Mapping erwartet")
                    continue
                errs.extend(_validate_trigger(lname, i, trig, spec))
            errs += _validate_worktree(lname, ldef, steps)
            if "retry" in ldef:
                errs += _validate_retry_block(ldef["retry"], f"loop {lname}.retry")
            errs += _validate_retry_cron(spec, lname, ldef)
            for i, st in enumerate(steps):
                if not isinstance(st, dict) or st.get("type") not in config.STEP_TYPES:
                    errs.append(f"loop {lname} step {i}: type muss einer von {config.STEP_TYPES} sein")
                if isinstance(st, dict):
                    where = f"loop {lname} step {i}"
                    errs += _validate_approval_when(st, where)
                    if extra_context.KEY in st:
                        errs += extra_context.validate(st[extra_context.KEY], where, st.get("type"))
                    if "approval_reason" in st and not isinstance(st["approval_reason"], str):
                        errs.append(f"{where}: approval_reason muss ein String (Template) sein")
                    if "failure_kind" in st:
                        errs += _validate_failure_kind(st, where)
                    stype = st.get("type")
                    validator = _STEP_VALIDATORS.get(stype) if isinstance(stype, str) else None
                    if validator is not None:
                        errs += validator(st, where, spec)
    if errs:
        raise ValueError("Spec ungültig:\n  " + "\n  ".join(errs))


def _as_number(value: object) -> float:
    """Zahl oder Zahl-String (aus `vars`) als float; Bool/sonstiges → NaN."""
    if isinstance(value, bool):
        return math.nan
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.nan


def _validate_poll(value: object, where: str) -> list[str]:
    """Optionales `poll`-Intervall: endliche Zahl >= 0 (auch als Zahl-String aus `vars`), kein Bool.

    Der Scheduler rechnet mit `float(poll)`; ungültige Werte würden sonst erst im
    Tick mit ValueError auffallen bzw. ein NaN-Intervall nie/immer feuern lassen."""
    if value is None:
        return []
    seconds = _as_number(value)
    if not math.isfinite(seconds) or seconds < 0:
        return [f"{where}: poll muss eine endliche Zahl >= 0 (Sekunden) sein (ist {value!r})"]
    return []


def _validate_positive_int(trig: dict, key: str, where: str) -> list[str]:
    """Optionales Trigger-Feld `key`: ganze Zahl >= 1 (auch als Zahl-String aus `vars`), kein Bool."""
    value = trig.get(key)
    if value is None:
        return []
    number = _as_number(value)
    if not math.isfinite(number) or number < 1 or not number.is_integer():
        return [f"{where}: {key} muss eine ganze Zahl >= 1 sein (ist {value!r})"]
    return []


def _validate_trigger(lname: str, i: int, trig: dict, spec: dict) -> list[str]:
    """Validiert einen Trigger-Def; liefert eine Liste von Fehlermeldungen.

    `on_issue`/`on_pr` brauchen ein Repo (Trigger- oder Spec-Ebene) — ohne
    Repo würde der Trigger stumm nie feuern (Config-Fehler statt Silent-Noop).
    """
    errs = []
    ttype = trig.get("type")
    if ttype not in config.TRIGGER_TYPES:
        errs.append(f"loop {lname} trigger {i}: type muss einer von {config.TRIGGER_TYPES} sein (ist {ttype!r})")
        return errs
    if ttype == "on_slack":
        errs += slack.validate_trigger(trig, f"loop {lname} trigger {i}")
        for key in ("max_pages", "max_alert_chars"):
            errs += _validate_positive_int(trig, key, f"loop {lname} trigger {i}")
    if ttype in config.POLL_TRIGGER_TYPES:
        errs += _validate_poll(trig.get("poll"), f"loop {lname} trigger {i}")
    if ttype in ("on_issue", "on_pr") and not (trig.get("repo") or spec.get("github")):
        errs.append(f"loop {lname} trigger {i}: {ttype} ohne repo (Trigger- oder Spec-Ebene)")
    if ttype == alerts.TRIGGER_TYPE:
        errs += alerts.validate_trigger(trig, f"loop {lname} trigger {i}")
    if trig.get("skip_blocked") and not (ttype == "on_issue" and trig.get("reconcile")):
        # Event-Modus feuert jedes Issue nur einmal — ein blockiertes Issue ginge
        # verloren statt nach dem Schließen des Blockers zu starten (ADR 0031).
        errs.append(f"loop {lname} trigger {i}: skip_blocked nur für on_issue mit reconcile: true")
    on_start = trig.get("on_start")
    if on_start is not None and (ttype != "cron" or not isinstance(on_start, bool)):
        errs.append(f"loop {lname} trigger {i}: on_start nur für cron und nur true/false (ist {on_start!r})")
    if ttype == "on_pr":
        states = trig.get("states")
        if states is not None and (
            not isinstance(states, list) or not states or any(s not in config.PR_STATES for s in states)
        ):
            errs.append(
                f"loop {lname} trigger {i}: on_pr states muss nicht-leere Liste aus {list(config.PR_STATES)} "
                f"sein (ist {states!r})"
            )
        rec = trig.get("reconcile")
        if rec is not None and not isinstance(rec, bool) and rec != config.RECONCILE_PER_HEAD:
            errs.append(
                f"loop {lname} trigger {i}: on_pr reconcile muss true/false oder "
                f"{config.RECONCILE_PER_HEAD!r} sein (ist {rec!r})"
            )
    return errs


def collect_projects(files: list[Path] | None = None, *, check_root: bool = True) -> tuple[dict[str, dict], list[str]]:
    """Specs aller Quellen (spec_sources.spec_files) + Namenskonflikte.

    Bei gleichem Namen gewinnt die Quelle mit der höheren Priorität; jede verdeckte
    Spec erzeugt eine Konfliktmeldung (``fabrik validate`` schlägt dann fehl).
    ``files`` ersetzt die Quellen (z. B. CI: nur die Specs im Repo); ``check_root`` wie
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
            conflicts.append(f"Projekt {name!r} mehrfach: {projects[name].get('_path') or '?'} (gewinnt) verdeckt {f}")
            continue
        spec.setdefault("_path", str(f))
        projects[name] = spec
    return projects, conflicts


def load_projects() -> dict[str, dict]:
    """Lädt alle Specs; defekte Specs werden mit _error gemarkt.

    Branch-Overrides (state/branch-overrides.json, ADR 0004) werden am Ende
    darübergelegt, damit Server/Engine/CLI den zur Laufzeit gewählten Branch sehen.
    """
    return branches.apply_overrides(collect_projects()[0])
