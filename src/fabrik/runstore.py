"""Run-Store (ADR 0034): Operationen mit Vorbedingung, JSON-Backend und aktives Backend (``STORE``).

:class:`BaseRunStore` definiert die Operationen backend-neutral; :class:`RunStore` ist das
JSON-Backend (`runs/<projekt>/<id>.json`), :class:`fabrik.runstore_sql.SqlRunStore` das relationale
(Schritt 3). ``STORE`` delegiert zur Aufrufzeit an das per ``FABRIK_RUN_STORE`` gewählte Backend.

Schritt 1 (Lesezugriff): Das Laden ist tolerant gegenüber Altbeständen, aber nie still: ein
Record, der sich nicht ins Modell abbilden lässt, wird mit Grund gemeldet (:class:`LoadReport`).

Schritt 2 (Schreibzugriff): Alle Run-Record-Write-Operationen laufen als Transaktion mit
Vorbedingung über den Store (ADR 0034 Nr. 2): Projekt-Lock → On-Disk-Stand lesen → Vorbedingung
prüfen (erlaubter Statusübergang laut ``RUN_TRANSITIONS``, optional erwarteter On-Disk-Status
``expect`` als Compare-and-Set) → redigiert atomar schreiben (Temp-Datei + `Path.replace`).
Gleicher Status ist nur für nicht-terminale Status ein erlaubter "Übergang" (Checkpoints,
Approve-Updates); ein Terminal-Record wird nicht erneut überschrieben. Ein unerlaubter oder veralteter Übergang wirft
:class:`PreconditionError` und lässt den Record unverändert. Außerhalb des Stores wird kein
Run-JSON mehr direkt geschrieben.

Defaults für fehlende oder falsch typisierte Felder:

- ``id`` und ``status`` (gültiger :class:`RunStatus`) sind Pflicht; ``project`` ist der Verzeichnisname
  (Ablageort hat Vorrang vor dem Feld im Record).
- Text-Felder (``loop``, ``trigger``, ``note``, ``spec_hash``, ``started``, ``finished``, ``error``,
  ``waiting_step``, ``approval_queued``, ``retry_of``, ``worktree_cleanup_error``): ``""``;
  Nicht-Strings → ``""``.
- ``started_by``/``approved_by``/``rejected_by``: ``None`` (Records vor der Authentifizierung, ADR 0030).
- ``resumed``: ``False``; ``vars``: ``{}``, nur skalare Werte (str/int/float/bool) bleiben;
  schreibgeschützt (:class:`types.MappingProxyType`), da :class:`~fabrik.runmodel.Run` unveränderlich ist.
- ``steps``: ``[]``; Nicht-Objekte in der Liste werden übersprungen, ein Step ohne gültigen Status ist
  ein Ladefehler. ``name``/``type``/``summary``/``output``: ``""``; ``duration_s``: ``None``.
- ``usage``: fehlende Zahlen ``0``, ``cost_usd`` ``None``. ``review``: ``round`` 1, ``pr`` ``None``.
- ``approval``: ``reason`` fehlt/unbekannt → ``tier`` bei ``waiting_step == "tier-approval"``, sonst ``step``.
  Der :class:`ApprovalStatus` ist im Record nicht gespeichert und wird abgeleitet: Run ``rejected`` →
  ``rejected``; Run ``needs_approval`` → ``queued`` (``approval_queued`` gesetzt) bzw. ``pending``;
  sonst ``approved``, wenn der Run fortgesetzt wurde (``resumed``/``approved_by``), andernfalls ``pending``.
- ``failure_kind`` (ADR 0035): bei gescheiterten Runs (``fail``/``error``) bzw. Steps (``fail``/``timeout``/
  ``error``) fehlend oder unbekannt → ``unknown`` (Altbestand, kein Backfill); sonst ``None``.
  ``retry_after``: ``""``.
- ``attempt`` (ADR 0036): ``0``; nur nicht-negative Ganzzahlen bleiben, sonst ``0``.
- ``steps_output`` und unbekannte Felder werden ignoriert.
"""

from __future__ import annotations

import contextlib
import json
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar

from . import config, filelock
from .failures import Failure, parse_kind
from .runmodel import (
    FAILED_RUN_STATUSES,
    FAILED_STEP_STATUSES,
    RUN_TRANSITIONS,
    TERMINAL_RUN_STATUSES,
    TIER_APPROVAL_STEP,
    Approval,
    ApprovalReason,
    ApprovalStatus,
    FailureKind,
    Finding,
    ReviewResult,
    Run,
    RunStatus,
    Step,
    StepStatus,
    Usage,
    VerifyCheck,
)
from .runquery import RunQuery, sort_key
from .secrets import _redact_record

_E = TypeVar("_E", bound=Enum)

# Lock-Datei je Projektverzeichnis (``<name>.lock``, kein ``*.json`` → nie als Record geladen).
_PROJECT_LOCK = ".runs"


class RecordError(ValueError):
    """Ein Record lässt sich nicht ins Modell abbilden (Text = Grund)."""


class RunStoreError(Exception):
    """Fehler beim Schreiben eines Run-Records (defekter On-Disk-Stand, I/O)."""


class PreconditionError(RunStoreError):
    """Vorbedingung einer Write-Operation verletzt: der On-Disk-Status (bzw. -Zustand) des Runs
    erlaubt den Übergang nicht. Der Record bleibt unverändert (ADR 0034 Nr. 2)."""


def _require_status(raw: dict, run_id: str, status: RunStatus) -> None:
    """On-Disk-Status muss ``status`` sein (sonst :class:`PreconditionError`)."""
    actual = raw.get("status")
    if actual != status.value:
        raise PreconditionError(f"Run {run_id}: On-Disk-Status {actual!r}, erwartet {status.value!r}")


def valid_run_id(run_id: str) -> bool:
    """Run-Ids landen direkt im Dateipfad (`runs/<projekt>/<id>.json|log`) → Pfad-Traversal ablehnen."""
    return bool(run_id) and "/" not in run_id and "\\" not in run_id and ".." not in run_id


def valid_project(project: str) -> bool:
    """Projektnamen landen als Pfadkomponente unter RUNS_DIR → nur ein einzelnes Segment erlauben."""
    return bool(project) and project not in (".", "..") and "/" not in project and "\\" not in project


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _opt_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _enum(enum_cls: type[_E], value: Any, what: str) -> _E:
    try:
        return enum_cls(value)
    except ValueError:
        raise RecordError(f"{what}: unbekannter Status {value!r}") from None


def _parse_usage(d: Any) -> Usage | None:
    if not isinstance(d, dict):
        return None
    return Usage(
        model=_text(d.get("model")),
        provider=_text(d.get("provider")),
        prompt_tokens=_int(d.get("prompt_tokens")),
        completion_tokens=_int(d.get("completion_tokens")),
        total_tokens=_int(d.get("total_tokens")),
        cost_usd=_num(d.get("cost_usd")),
    )


def _pr_number(value: Any) -> int | None:
    """PR-Nummer als int (Records speichern int oder numerischen String); sonst None."""
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_review(d: Any) -> ReviewResult | None:
    if not isinstance(d, dict):
        return None
    return ReviewResult(
        verdict=_text(d.get("verdict")),
        model=_text(d.get("model")),
        summary=_text(d.get("summary")),
        pr=_pr_number(d.get("pr")),
        round=_int(d.get("round"), 1),
        findings=tuple(
            Finding(_text(f.get("severity")), _text(f.get("text")), _text(f.get("suggestion")))
            for f in _dicts(d.get("findings"))
        ),
    )


def _failure_kind(d: Mapping[str, Any], failed: bool) -> FailureKind | None:
    """``failure_kind`` nur für gescheiterte Runs/Steps; Altbestand ohne Feld → ``unknown``."""
    return parse_kind(d.get("failure_kind")) if failed else None


def _parse_step(d: Mapping[str, Any], index: int) -> Step:
    status = _enum(StepStatus, d.get("status"), f"Step {index}")
    return Step(
        name=_text(d.get("name")),
        type=_text(d.get("type")),
        status=status,
        duration_s=_num(d.get("duration_s")),
        summary=_text(d.get("summary")),
        output=_text(d.get("output")),
        usage=_parse_usage(d.get("usage")),
        review=_parse_review(d.get("review")),
        verify=tuple(
            VerifyCheck(_text(c.get("name")), bool(c.get("ok")), _text(c.get("output")))
            for c in _dicts(d.get("verify"))
        ),
        failure_kind=_failure_kind(d, status in FAILED_STEP_STATUSES),
        retry_after=_text(d.get("retry_after")),
        verdict=_text(d.get("verdict")),
    )


def _approval_status(run_status: RunStatus, rec: Mapping[str, Any]) -> ApprovalStatus:
    if run_status is RunStatus.REJECTED:
        return ApprovalStatus.REJECTED
    if run_status is RunStatus.NEEDS_APPROVAL:
        return ApprovalStatus.QUEUED if _text(rec.get("approval_queued")) else ApprovalStatus.PENDING
    if rec.get("resumed") is True or _text(rec.get("approved_by")):
        return ApprovalStatus.APPROVED
    return ApprovalStatus.PENDING


def _parse_approval(d: Any, run_status: RunStatus, rec: Mapping[str, Any]) -> Approval | None:
    if not isinstance(d, dict):
        return None
    try:
        reason = ApprovalReason(_text(d.get("reason")))
    except ValueError:
        tier = rec.get("waiting_step") == TIER_APPROVAL_STEP
        reason = ApprovalReason.TIER if tier else ApprovalReason.STEP
    tier_no = d.get("tier")
    return Approval(
        status=_approval_status(run_status, rec),
        reason=reason,
        kind=_text(d.get("kind")),
        what=_text(d.get("what")),
        step=_text(d.get("step")),
        issue=_text(d.get("issue")),
        condition=_text(d.get("condition")),
        why=_text(d.get("why")),
        tier=tier_no if isinstance(tier_no, int) and not isinstance(tier_no, bool) else None,
    )


def attempt_of(record: Mapping[str, Any] | None) -> int:
    """0-basierte Retry-Versuchszahl aus einem Run-Record (ADR 0036).

    Einzige Quelle für das ``attempt``-Feld: nur nicht-negative Ganzzahlen
    bleiben; fehlend, defekt (String, Bool) oder negativ → ``0``. Wird von
    :func:`parse_run` und :mod:`fabrik.retry_chains` geteilt."""
    if not isinstance(record, Mapping):
        return 0
    attempt = record.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        return 0
    return attempt


def parse_run(rec: Mapping[str, Any], project: str | None = None) -> Run:
    """Run-Record (dict aus dem JSON) → :class:`Run`; :class:`RecordError` mit Grund, wenn unmöglich.

    ``project``: Verzeichnisname, hat Vorrang vor dem Feld im Record. Defaults: siehe Moduldoku."""
    run_id = rec.get("id")
    if not isinstance(run_id, str) or not run_id:
        raise RecordError("id fehlt oder ist kein nicht-leerer String")
    proj = project or _text(rec.get("project"))
    if not proj:
        raise RecordError("project fehlt")
    status = _enum(RunStatus, rec.get("status"), "Run")
    steps = tuple(_parse_step(s, i) for i, s in enumerate(_dicts(rec.get("steps")), 1))
    raw_vars = rec.get("vars")
    # MappingProxyType: Run.vars bleibt schreibgeschützt (Run ist frozen, ADR 0034).
    run_vars = MappingProxyType(
        {k: v for k, v in raw_vars.items() if isinstance(v, (str, int, float, bool))}
        if isinstance(raw_vars, dict)
        else {}
    )
    return Run(
        id=run_id,
        project=proj,
        loop=_text(rec.get("loop")),
        status=status,
        started=_text(rec.get("started")),
        finished=_text(rec.get("finished")),
        trigger=_text(rec.get("trigger")),
        note=_text(rec.get("note")),
        spec_hash=_text(rec.get("spec_hash")),
        steps=steps,
        approval=_parse_approval(rec.get("approval"), status, rec),
        waiting_step=_text(rec.get("waiting_step")),
        resumed=rec.get("resumed") is True,
        approval_queued=_text(rec.get("approval_queued")),
        started_by=_opt_text(rec.get("started_by")),
        approved_by=_opt_text(rec.get("approved_by")),
        rejected_by=_opt_text(rec.get("rejected_by")),
        error=_text(rec.get("error")),
        failure_kind=_failure_kind(rec, status in FAILED_RUN_STATUSES),
        retry_after=_text(rec.get("retry_after")),
        retry_of=_text(rec.get("retry_of")),
        attempt=attempt_of(rec),
        worktree_cleanup_error=_text(rec.get("worktree_cleanup_error")),
        vars=run_vars,
    )


@dataclass(frozen=True)
class LoadFailure:
    """Ein nicht ladbarer Record: Ort (Datei bzw. ``<projekt>/<id>``) + Grund."""

    path: Path
    reason: str


@dataclass
class LoadReport:
    """Ergebnis eines Gesamtlaufs über alle Records: Anzahl geladen + Fehlschläge mit Grund."""

    loaded: int = 0
    failures: list[LoadFailure] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Anzahl gelesener Records."""
        return self.loaded + len(self.failures)


def _check_transition(raw: Mapping[str, Any], record: Mapping[str, Any], expect: RunStatus | None = None) -> None:
    """Gespeicherter Status → Record-Status muss in ``RUN_TRANSITIONS`` liegen oder für einen
    nicht-terminalen Status identisch sein (Checkpoint-/Approve-Updates schreiben denselben
    Status). ``expect``: der gespeicherte Status muss genau dieser sein (Compare-and-Set auf den
    Stand, den der Aufrufer gelesen hat — z. B. schließt ``needs_approval → running`` einen
    zweiten Resume aus, ``queued → error`` überschreibt keinen inzwischen laufenden Run).

    Unbekannter gespeicherter Status → :class:`RunStoreError` (defekter Record);
    unerlaubter Übergang → :class:`PreconditionError`."""
    cur, tgt = raw.get("status"), record.get("status")
    if not isinstance(cur, str) or not isinstance(tgt, str):
        raise RunStoreError(f"defekter Run-Record: unbekannter Status {cur!r}")
    try:
        current = RunStatus(cur)
        target = RunStatus(tgt)
    except ValueError as e:
        raise RunStoreError(f"defekter Run-Record: unbekannter Status {cur!r}") from e
    run_id = record.get("id") or "?"
    if expect is not None and current is not expect:
        raise PreconditionError(f"Run {run_id}: On-Disk-Status {current.value!r}, erwartet {expect.value!r}")
    same = target is current and current not in TERMINAL_RUN_STATUSES
    if not same and target not in RUN_TRANSITIONS[current]:
        raise PreconditionError(f"Run {run_id}: unerlaubter Übergang {current.value!r} → {target.value!r}")


def _existing(raw: dict | None, run_id: str) -> dict:
    if raw is None:
        raise PreconditionError(f"Run {run_id} existiert nicht")
    return raw


# Mutation eines Records innerhalb einer Store-Transaktion: gespeicherter Stand (None = fehlt) → neuer Stand.
Mutation = Callable[[dict | None], dict]


class BaseRunStore(ABC):
    """Backend-neutrale Run-Store-Operationen (ADR 0034 Nr. 3).

    Jede Write-Operation ist eine :data:`Mutation`, die das Backend in :meth:`_modify` atomar
    ausführt: gespeicherten Stand exklusiv lesen → Vorbedingung prüfen → redigiert schreiben. Ein
    unerlaubter oder veralteter Übergang wirft :class:`PreconditionError` und lässt den Run
    unverändert. Leseoperationen liefern rohe Records (``dict``, für Engine/API) oder das
    typisierte Modell (:class:`~fabrik.runmodel.Run`)."""

    # ------------------------------------------------------------ Backend-Primitive
    @abstractmethod
    def _modify(self, project: str, run_id: str, mutate: Mutation) -> dict:
        """``mutate`` exklusiv auf den gespeicherten Stand anwenden und das Ergebnis redigiert
        speichern (in-place, siehe :func:`fabrik.secrets._redact_record`); liefert es zurück.
        Ungültige Projekt-/Run-Ids → :class:`RunStoreError`."""

    @abstractmethod
    def get_record(self, run_id: str, project: str | None = None) -> dict | None:
        """Roher Record per Id (optional im Projekt); None, wenn unbekannt oder Id ungültig."""

    @abstractmethod
    def load_records(self, project: str | None = None, status: RunStatus | None = None) -> list[dict]:
        """Rohe Records (ohne ``steps_output``, ``project`` = Ablageort) aller bzw. eines Projekts,
        optional nur mit ``status``; unsortiert. Nicht lesbare Records werden übersprungen."""

    @abstractmethod
    def load_report(self, project: str | None = None) -> tuple[list[Run], LoadReport]:
        """Alle Runs typisiert laden; nicht abbildbare landen mit Grund im Report."""

    # ------------------------------------------------------------ Lesen
    def load_all(self, project: str | None = None) -> list[Run]:
        """Alle ladbaren Runs (unsortiert); nicht ladbare Records werden übersprungen (siehe :meth:`load_report`)."""
        return self.load_report(project)[0]

    def get(self, run_id: str, project: str | None = None) -> Run | None:
        """Run per Id (optional im Projekt); None, wenn unbekannt, ungültig oder nicht ladbar."""
        try:
            rec = self.get_record(run_id, project)
            return parse_run(rec, project or None) if rec is not None else None
        except (RecordError, RunStoreError):
            return None

    def loop_triggers(self, project: str, loop: str, statuses: tuple[str, ...] | None = None) -> set[str]:
        """Trigger-Strings aller Runs von ``project``/``loop`` (optional nur mit einem der ``statuses``)."""
        return {
            str(r.get("trigger", ""))
            for r in self.load_records(project)
            if r.get("loop") == loop and (statuses is None or r.get("status") in statuses)
        }

    def search(self, query: RunQuery) -> list[Run]:
        """Treffer von ``query``, neueste zuerst (:func:`~fabrik.runquery.sort_key`), höchstens
        ``query.limit + 1`` (der überzählige zeigt eine Folgeseite an).

        Records mit unbekanntem Status sind nicht im Modell darstellbar und daher nicht
        in den Treffern (nur über :meth:`load_report` sichtbar, ADR 0034)."""
        hits = sorted(filter(query.matches, self.load_all(query.project)), key=sort_key, reverse=True)
        return hits[: query.limit + 1]

    # ------------------------------------------------------------ Schreiben (ADR 0034, Schritt 2)
    def _write_with_precondition(
        self,
        project: str,
        run_id: str,
        record: dict,
        *,
        create: bool = False,
        allow_missing: bool = False,
        expect: RunStatus | None = None,
    ) -> dict:
        """Record schreiben, wenn die Vorbedingung am gespeicherten Stand gilt.

        ``create=True``: der Run darf nicht existieren (Neuanlage). ``allow_missing=True``: er
        darf fehlen (Neuanlage ohne Existenzprüfung, z. B. queued-Platzhalter, den `run_loop`
        beim Start überschreibt). Sonst muss der gespeicherte Status laut ``RUN_TRANSITIONS``
        zum Record-Status übergehen können (siehe :func:`_check_transition`, ``expect``:
        erwarteter Status). Liefert den übergebenen Record; :class:`PreconditionError`, wenn verletzt."""

        def mutate(raw: dict | None) -> dict:
            if create:
                if raw is not None:
                    raise PreconditionError(f"Run {run_id} existiert bereits (Status {raw.get('status')!r})")
            elif raw is None:
                if not allow_missing:
                    raise PreconditionError(f"Run {run_id} existiert nicht")
            else:
                _check_transition(raw, record, expect)
            return record

        return self._modify(project, run_id, mutate)

    def create(self, project: str, run_id: str, record: dict) -> dict:
        """Neuen Record anlegen; der Run darf nicht existieren (sonst :class:`PreconditionError`)."""
        return self._write_with_precondition(project, run_id, record, create=True)

    def write(
        self,
        project: str,
        run_id: str,
        record: dict,
        *,
        allow_missing: bool = False,
        expect: RunStatus | None = None,
    ) -> dict:
        """Record schreiben (Engine-Checkpoints); der Übergang vom gespeicherten Status muss laut
        ``RUN_TRANSITIONS`` erlaubt sein (oder für einen nicht-terminalen Status identisch sein).

        ``allow_missing=True``: der Run darf auch fehlen (Neuanlage, z. B. queued-Platzhalter,
        den `run_loop` beim Start überschreibt). ``expect``: der gespeicherte Status muss genau
        dieser sein (sonst :class:`PreconditionError`)."""
        return self._write_with_precondition(project, run_id, record, allow_missing=allow_missing, expect=expect)

    def approve(self, project: str, run_id: str, record: dict, actor: str | None = None) -> dict:
        """Freigabe-Audit: ``approved_by`` setzen (Status bleibt ``needs_approval``).

        Nur von ``needs_approval``. ``approved_by`` ist reines Audit, keine Sperre: scheitert der
        Resume danach (oder wird er verworfen), darf erneut freigegeben oder abgelehnt werden.
        Exklusiv ist erst der Resume selbst (``needs_approval → running`` mit ``expect``, siehe
        :meth:`write`) — ein gleichzeitiger Reject gewinnt oder verliert dort."""
        if actor:
            record["approved_by"] = actor
        record["status"] = RunStatus.NEEDS_APPROVAL.value
        return self._write_with_precondition(project, run_id, record, expect=RunStatus.NEEDS_APPROVAL)

    def reject(self, project: str, run_id: str, record: dict, actor: str | None = None) -> dict:
        """Ablehnung entscheiden: Status ``rejected`` + ``rejected_by`` setzen.

        Nur von ``needs_approval`` — auch bei einer wartenden Freigabe (``approval_queued``):
        der Worker prüft den Status nach dem Slot-Erwerb und setzt nicht mehr fort. Der
        Queue-Marker wird entfernt."""
        if actor:
            record["rejected_by"] = actor
        record["status"] = RunStatus.REJECTED.value
        record.pop("approval_queued", None)
        return self._write_with_precondition(project, run_id, record, expect=RunStatus.NEEDS_APPROVAL)

    def mark_approval_queued(self, project: str, run_id: str, record: dict, actor: str | None = None) -> dict:
        """Freigabe eingereiht (kein Slot frei): ``approval_queued`` + ``approved_by`` setzen (ADR 0015).

        Nur von ``needs_approval`` (sonst :class:`PreconditionError`)."""
        record["approval_queued"] = config.utcnow().isoformat()
        if actor:
            record["approved_by"] = actor
        record["status"] = RunStatus.NEEDS_APPROVAL.value
        return self._write_with_precondition(project, run_id, record, expect=RunStatus.NEEDS_APPROVAL)

    def revoke_approval(self, project: str, run_id: str) -> dict:
        """Freigabe zurücknehmen, ohne dass ein Resume begonnen hat (Worker nicht gestartet bzw.
        verworfen): ``approved_by`` leeren, ``approval_queued`` entfernen.

        Read-Modify-Write, nur von ``needs_approval`` — ein inzwischen fortgesetzter oder
        abgelehnter Run bleibt unberührt (:class:`PreconditionError`)."""

        def mutate(raw: dict | None) -> dict:
            raw = _existing(raw, run_id)
            _require_status(raw, run_id, RunStatus.NEEDS_APPROVAL)
            raw["approved_by"] = None
            raw.pop("approval_queued", None)
            return raw

        return self._modify(project, run_id, mutate)

    def update_fields(self, project: str, run_id: str, fields: dict) -> dict:
        """Bestehenden Record um ``fields`` ergänzen (Read-Modify-Write).

        Der gespeicherte Stand wird neu gelesen und nur die übergebenen Felder gesetzt —
        zwischenzeitliche Änderungen (z. B. ``approved_by``/``approval_queued`` durch einen
        Approve) gehen nicht verloren. Der Status bleibt unverändert; ein fehlender Run
        wirft :class:`PreconditionError`."""

        def mutate(raw: dict | None) -> dict:
            raw = _existing(raw, run_id)
            raw.update(fields)
            return raw

        return self._modify(project, run_id, mutate)

    def reap(self, project: str, run_id: str, *, error: str) -> dict:
        """Running-Run von außen abbrechen (Reaper, Dead-Man's-Switch): Status ``error`` + ``interrupted``.

        Read-Modify-Write: nur die Fehler-Felder werden auf den aktuellen gespeicherten Stand
        angewendet, ein zwischenzeitlicher Engine-Checkpoint geht nicht verloren. Nur von
        ``running`` — ein Run, den die Engine bereits beendet hat, wird nicht überschrieben
        (sonst :class:`PreconditionError`)."""

        def mutate(raw: dict | None) -> dict:
            raw = _existing(raw, run_id)
            _require_status(raw, run_id, RunStatus.RUNNING)
            raw["status"] = RunStatus.ERROR.value
            raw["error"] = error
            raw.update(Failure(FailureKind.INTERRUPTED).fields())
            raw["finished"] = config.utcnow().isoformat()
            return raw

        return self._modify(project, run_id, mutate)


def _check_ids(project: str, run_id: str) -> None:
    """Ungültige Projekt-/Run-Ids (Pfad-Traversal) → :class:`RunStoreError`."""
    if not valid_project(project):
        raise RunStoreError(f"ungültiger Projektname: {project!r}")
    if not valid_run_id(run_id):
        raise RunStoreError(f"ungültige Run-Id: {run_id!r}")


# Datei-Identität für den Parse-Cache: (mtime_ns, size, inode).
FileStamp = tuple[int, int, int]


class RunStore(BaseRunStore):
    """JSON-Backend: Run-Records unter ``runs_dir`` (Default ``config.RUNS_DIR``, zur Aufrufzeit).

    Writes laufen unter einem Projekt-Lock (Lock-Datei + Thread-Lock) und schreiben atomar
    (Temp-Datei + `Path.replace`).

    Lesen cacht geparste Runs je Datei, gültig solange ``(mtime_ns, size, inode)`` gleich bleibt:
    Suche/Listen scannen sonst bei jeder Anfrage alle Records neu (JSON + ``parse_run`` ≈ O(n)).
    Atomare Writes erzeugen eine neue Inode, auch externe Änderungen fallen so auf."""

    def __init__(self, runs_dir: Path | None = None):
        self._runs_dir = runs_dir
        self._parsed: dict[Path, tuple[FileStamp, Run]] = {}
        self._parsed_lock = threading.Lock()

    @property
    def root(self) -> Path:
        """Wurzelverzeichnis der Records."""
        return self._runs_dir or config.RUNS_DIR

    def _dirs(self, project: str | None) -> list[Path]:
        if project:
            return [self.root / project] if valid_project(project) else []
        return sorted(self.root.iterdir()) if self.root.is_dir() else []

    def _files(self, project: str | None) -> Iterator[Path]:
        for d in self._dirs(project):
            if d.is_dir():
                yield from d.glob("*.json")

    def _read(self, f: Path) -> Run:
        """Datei → Run (gecacht, siehe Klasse); :class:`RecordError` bei defektem JSON/Struktur.
        Andere I/O-Fehler propagieren."""
        try:
            st = f.stat()
        except FileNotFoundError:
            raise RecordError("Datei verschwunden") from None
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        cached = self._parsed.get(f)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        run = self._parse_file(f)
        with self._parsed_lock:
            self._parsed[f] = (stamp, run)
        return run

    @staticmethod
    def _parse_file(f: Path) -> Run:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RecordError("Datei verschwunden") from None
        except ValueError as e:  # JSONDecodeError und UnicodeDecodeError
            raise RecordError(f"kein gültiges JSON: {e}") from e
        if not isinstance(data, dict):
            raise RecordError(f"erwartet Object, ist {type(data).__name__}")
        return parse_run(data, f.parent.name)

    def load_report(self, project: str | None = None) -> tuple[list[Run], LoadReport]:
        """Alle Records laden; nicht ladbare landen mit Grund im Report statt still verloren zu gehen."""
        runs: list[Run] = []
        report = LoadReport()
        seen: set[Path] = set()
        for f in self._files(project):
            seen.add(f)
            try:
                runs.append(self._read(f))
            except RecordError as e:
                report.failures.append(LoadFailure(f, str(e)))
        self._forget_unseen(set(self._dirs(project)), seen)
        report.loaded = len(runs)
        return runs, report

    def _forget_unseen(self, scanned: set[Path], seen: set[Path]) -> None:
        """Cache-Einträge gelöschter Records (Datei in einem gescannten Verzeichnis, aber nicht mehr da) entfernen."""
        with self._parsed_lock:
            for f in [f for f in self._parsed if f.parent in scanned and f not in seen]:
                del self._parsed[f]

    def get_record(self, run_id: str, project: str | None = None) -> dict | None:
        """Roher Record per Id (``project`` = Ablageort); defekter Record → :class:`RunStoreError`."""
        if not valid_run_id(run_id):
            return None
        for d in self._dirs(project):
            raw = self._read_raw(d / f"{run_id}.json") if d.is_dir() else None
            if raw is not None:
                raw["project"] = d.name
                return raw
        return None

    def load_records(self, project: str | None = None, status: RunStatus | None = None) -> list[dict]:
        out = []
        for f in self._files(project):
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
            except (
                ValueError,
                FileNotFoundError,
            ) as e:  # defektes JSON / Race mit Löschen; andere I/O-Fehler propagieren
                print(f"[fabrik] Run-Record {f.name} übersprungen: {e}")
                continue
            if not isinstance(r, dict):
                print(f"[fabrik] Run-Record {f.name} übersprungen: erwartet Object, ist {type(r).__name__}")
                continue
            if status is not None and r.get("status") != status.value:
                continue
            r.pop("steps_output", None)
            r["project"] = f.parent.name  # Ablageort hat Vorrang (wie parse_run)
            out.append(r)
        return out

    def raw_records(self) -> Iterator[tuple[Path, dict | RunStoreError]]:
        """Jede Record-Datei roh, sortiert (Quelle der Migration, #278): (Pfad, Record) bzw.
        (Pfad, Fehler mit Grund) für defekte Dateien; zwischendurch gelöschte fallen weg."""
        for f in sorted(self._files(None)):
            try:
                raw = self._read_raw(f)
            except RunStoreError as e:
                yield f, e
                continue
            if raw is not None:
                yield f, raw

    # ------------------------------------------------------------ Schreibzugriff
    def _path(self, project: str, run_id: str) -> Path:
        """Pfad des Run-Records; ungültige Projekt-/Run-Ids werfen :class:`RunStoreError`."""
        _check_ids(project, run_id)
        return self.root / project / f"{run_id}.json"

    def _read_raw(self, path: Path) -> dict | None:
        """Rohes On-Disk-Record (dict) oder None, wenn die Datei fehlt. Defektes JSON → :class:`RunStoreError`."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError as e:  # JSONDecodeError und UnicodeDecodeError
            raise RunStoreError(f"defekter Run-Record {path.name}: {e}") from e
        if not isinstance(data, dict):
            raise RunStoreError(f"defekter Run-Record {path.name}: erwartet Object, ist {type(data).__name__}")
        return data

    def _atomic_write(self, path: Path, record: dict) -> None:
        """Record atomar schreiben (Temp-Datei + `Path.replace`); `OSError` propagiert.

        Die Redaction erfolgt **in-place**: der übergebene Record verliert seine Secrets, damit
        kein In-Memory-Stand (z. B. `rn.record` der Engine) Token trägt, die nur die On-Disk-Kopie
        maskiert hätte."""
        path.parent.mkdir(parents=True, exist_ok=True)
        _redact_record(record)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _lock(self, project: str) -> contextlib.AbstractContextManager[None]:
        """Exklusiver Lock für alle Records eines Projekts (``runs/<projekt>/.runs.lock``).

        Ein Lock pro Projekt statt pro Run: die Lock-Datei und der Thread-Lock in
        :mod:`fabrik.filelock` existieren nur einmal je Projekt (nicht je Run-Id, die nie
        aufgeräumt würden). Die geschützten Abschnitte sind kurz (lesen + atomar schreiben)."""
        return filelock.locked(self.root / project / _PROJECT_LOCK)

    def _modify(self, project: str, run_id: str, mutate: Mutation) -> dict:
        path = self._path(project, run_id)
        with self._lock(project):
            record = mutate(self._read_raw(path))
            self._atomic_write(path, record)
        return record


class ActiveRunStore:
    """Delegiert jeden Zugriff an das konfigurierte Backend (``FABRIK_RUN_STORE``, zur Aufrufzeit).

    Module binden ``STORE`` beim Import; das Backend (JSON oder SQL, :func:`fabrik.config.run_store_backend`)
    wird erst beim Attributzugriff aufgelöst — Tests und das Umschalten (#278) brauchen keinen Re-Import."""

    def backend(self) -> BaseRunStore:
        """Aktives Backend; der SQL-Store wird je DB-URL einmal angelegt (und migriert)."""
        if config.run_store_backend() == config.RUN_STORE_SQL:
            from .runstore_sql import store_for_url  # lokal: SQLAlchemy nur laden, wenn genutzt

            return store_for_url(config.db_url())
        return _JSON_STORE

    def __getattr__(self, name: str) -> Any:
        return getattr(self.backend(), name)


_JSON_STORE = RunStore()

# Gemeinsame Store-Instanz: Runs werden nur über den Store gelesen und geschrieben (ADR 0034).
STORE: Any = ActiveRunStore()
