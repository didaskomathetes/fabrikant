"""Retry-Ketten (ADR 0036): Wiederholungszustand je (Projekt, Loop, Trigger).

Eine Kette hält ``(project, loop, trigger_key)``, ``state``, ``next_attempt_at``,
``last_run_id`` und ``claimed_run_id`` — mehr nicht. Versuchszahl und Historie
stehen an den Runs (``attempt``, ``retry_of``) und werden über ``last_run_id``
abgeleitet. Der Zustandsautomat ist klein (``pending → exhausted | resolved |
dismissed``, ``exhausted → pending | resolved | dismissed``); die Runs behalten
ihren eigenen Automaten aus ADR 0034.

Ablage (JSON-Phase, bis zum SQL-Store #277): ``state/retry-chains.json`` unter
der exklusiven Dateisperre (:mod:`fabrik.filelock`), atomares Schreiben
(Temporäre Datei + ``rename``). Eindeutigkeit (höchstens eine ``pending``-Kette
je Schlüssel) und Claim (``claimed_run_id`` ``NULL`` → Run-Id, bedingt auf
``state = 'pending'`` und ``next_attempt_at <= now``) sind dieselben Operationen
wie im SQL-Store (ADR 0036, Entscheidung 3).

Der Claim gehört dem laufenden Versuch: nur der Run mit ``claimed_run_id``
entscheidet über die Kette, wenn er scheitert, und jedes Ende eines Versuchs
(Fehlschlag, Ablehnung, Neustart, Start nicht möglich) gibt ihn wieder frei.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import urllib.error
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from . import config, cron, pause
from .failures import parse_kind, parse_retry_after
from .filelock import locked
from .gitops import SPEC_ENV_FILE_KEY, project_github
from .retry import RetryPolicy
from .runmodel import FAILED_RUN_STATUSES, FailureKind, InvalidTransition, RunStatus, check_transition
from .runs import RunClaim, get_run, load_records
from .runstore import STORE, RunStoreError, attempt_of
from .specs import load_projects

# Kanonische Trigger-Schlüssel (ADR 0036, Begriffe): stabil über die Lebensdauer
# des Auftrags — nicht die Id des einzelnen Ticks/Events/Deliverys.
_ISSUE_TRIGGER_RE = re.compile(r"^on_issue:#(\d+)$")
_PR_TRIGGER_RE = re.compile(r"^on_pr:#(\d+)")
_GH_KEY_RE = re.compile(r"^(issue|pr):(.+)#(\d+)$")

# Ein fälliger Versuch, der nicht starten konnte (kein Slot, Loop pausiert,
# API-Fehler), wird um diese Zeit verschoben — sonst claimt ihn jeder
# Scheduler-Tick (2 s) erneut und fragt jedes Mal GitHub ab.
ATTEMPT_START_DEFER = timedelta(minutes=1)


class ChainState(StrEnum):
    """Zustand einer Retry-Kette (ADR 0036, Entscheidung 4)."""

    PENDING = "pending"
    EXHAUSTED = "exhausted"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


# Erlaubte Kette-Übergänge. Terminal: resolved, dismissed.
CHAIN_TRANSITIONS: dict[ChainState, frozenset[ChainState]] = {
    ChainState.PENDING: frozenset({ChainState.EXHAUSTED, ChainState.RESOLVED, ChainState.DISMISSED}),
    ChainState.EXHAUSTED: frozenset({ChainState.PENDING, ChainState.RESOLVED, ChainState.DISMISSED}),
    ChainState.RESOLVED: frozenset(),
    ChainState.DISMISSED: frozenset(),
}
_ACTIVE_STATES = frozenset({ChainState.PENDING, ChainState.EXHAUSTED})


class ClaimConflict(InvalidTransition):
    """Der Claim der Kette gehört nicht (mehr) dem erwarteten Run."""


class RetryBlocked(ValueError):
    """*Jetzt erneut* ist nicht erlaubt (Loop deaktiviert) oder konnte nicht starten."""


class ChainStoreCorrupt(ValueError):
    """``retry-chains.json`` ist nicht parsebar. Mutationen brechen ab, statt
    den Store mit dem Rest zu überschreiben (alle anderen Ketten wären weg)."""


@dataclass(frozen=True, slots=True)
class RetryChain:
    """Eine Retry-Kette. Zeitstempel sind ISO-Strings (``""`` = nicht gesetzt)."""

    project: str
    loop: str
    trigger_key: str
    state: ChainState
    next_attempt_at: str = ""
    last_run_id: str = ""
    claimed_run_id: str = ""  # "" = frei
    created: str = ""
    updated: str = ""

    @property
    def key(self) -> str:
        """Eindeutiger Schlüssel im Store (``::`` ist in Namen verboten, specs)."""
        return _store_key(self.project, self.loop, self.trigger_key)

    @property
    def is_claimed(self) -> bool:
        return bool(self.claimed_run_id)

    @property
    def is_active(self) -> bool:
        """``pending`` oder ``exhausted`` (nicht beendet)."""
        return self.state in _ACTIVE_STATES


def _store_key(project: str, loop: str, trigger_key: str) -> str:
    return f"{project}::{loop}::{trigger_key}"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


_UNSET = object()  # Sentinel: „Feld unverändert lassen“ (ChainStore.transition)


class ChainStore:
    """Lese-/Schreibzugriff auf die Retry-Ketten (JSON + Dateisperre).

    Alle Mutationen laufen unter der exklusiven Sperre auf der Store-Datei
    (prozessübergreifend, ``flock``) und schreiben atomar. ``path`` ist
    primär überschreibbar (Tests); Default ``config.STATE_DIR /
    "retry-chains.json"`` (zur Aufrufzeit)."""

    def __init__(self, path: Path | None = None):
        self._path = path

    @property
    def root(self) -> Path:
        return self._path or config.STATE_DIR / "retry-chains.json"

    def _load(self) -> dict[str, dict]:
        p = self.root
        if not p.is_file():
            return {}
        # Lesefehler (OSError) und defekter Inhalt propagieren: ein leerer
        # Fallback würde beim nächsten _save alle Ketten löschen.
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ChainStoreCorrupt(f"{p}: {e}") from e
        if not isinstance(data, dict):
            raise ChainStoreCorrupt(f"{p}: kein JSON-Objekt")
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _save(self, chains: dict[str, dict]) -> None:
        p = self.root
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(chains, indent=1, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())  # vor dem rename: kein abgeschnittener Store nach Stromausfall
        tmp.replace(p)

    @staticmethod
    def _parse(d: dict) -> RetryChain | None:
        try:
            state = ChainState(str(d.get("state")))
        except ValueError:
            return None
        return RetryChain(
            project=d.get("project", ""),
            loop=d.get("loop", ""),
            trigger_key=d.get("trigger_key", ""),
            state=state,
            next_attempt_at=str(d.get("next_attempt_at", "")),
            last_run_id=str(d.get("last_run_id", "")),
            claimed_run_id=str(d.get("claimed_run_id", "")),
            created=str(d.get("created", "")),
            updated=str(d.get("updated", "")),
        )

    def all(self) -> list[RetryChain]:
        """Alle Ketten (unsortiert); defekte Einträge werden übersprungen."""
        return [c for c in map(self._parse, self._load().values()) if c is not None]

    def get(self, project: str, loop: str, trigger_key: str) -> RetryChain | None:
        with locked(self.root):
            return self._parse(self._load().get(_store_key(project, loop, trigger_key), {}))

    def create(
        self,
        project: str,
        loop: str,
        trigger_key: str,
        *,
        next_attempt_at: datetime | None,
        last_run_id: str,
        now: datetime,
        state: ChainState = ChainState.PENDING,
    ) -> RetryChain:
        """Neue Kette anlegen (Default ``pending``; Bestandsabgleich: ``exhausted``).

        ValueError, wenn für den Schlüssel schon eine ``pending``-Kette existiert
        (Eindeutigkeit, ADR 0036 Entscheidung 3) oder ``state`` zu
        ``next_attempt_at`` passt nicht (``pending`` braucht einen Zeitpunkt,
        ``exhausted`` trägt keinen). Beendete Ketten (resolved/dismissed/
        exhausted) blockieren nicht — ein späterer Fehlschlag eröffnet eine neue
        Kette."""
        if state is ChainState.PENDING and next_attempt_at is None:
            raise ValueError(f"pending-Kette {project}::{loop}::{trigger_key} braucht next_attempt_at")
        if state is ChainState.EXHAUSTED and next_attempt_at is not None:
            raise ValueError(f"exhausted-Kette {project}::{loop}::{trigger_key} trägt kein next_attempt_at")
        key = _store_key(project, loop, trigger_key)
        with locked(self.root):
            chains = self._load()
            existing = self._parse(chains.get(key, {}))
            if existing is not None and existing.state is ChainState.PENDING:
                raise ValueError(f"Kette {key} ist bereits pending")
            chain = RetryChain(
                project=project,
                loop=loop,
                trigger_key=trigger_key,
                state=state,
                next_attempt_at=_iso(next_attempt_at) if next_attempt_at is not None else "",
                last_run_id=last_run_id,
                created=_iso(now),
                updated=_iso(now),
            )
            chains[key] = asdict(chain)
            self._save(chains)
        return chain

    def claim(
        self, project: str, loop: str, trigger_key: str, run_id: str, now: datetime, *, force: bool = False
    ) -> bool:
        """Einen fälligen Versuch claimen (ADR 0036, Entscheidung 3).

        Normal (``force=False``): gelingt nur, wenn die Kette ``pending`` ist,
        ``next_attempt_at <= now`` gilt und ``claimed_run_id`` frei ist — in
        einer einzigen Transaktion. Genau ein Starter gewinnt; alle anderen
        erhalten False und starten nichts.

        ``force=True`` (UI *jetzt erneut*): nur aus ``exhausted``, ohne
        Due-Check. Die Kette wird in derselben Transaktion wieder ``pending``
        (volles Budget, ``next_attempt_at`` leer) — der Claim bleibt beim neuen
        Run, bis dieser endet."""
        key = _store_key(project, loop, trigger_key)
        with locked(self.root):
            chains = self._load()
            chain = self._parse(chains.get(key, {}))
            if chain is None or chain.is_claimed:
                return False
            fields: dict[str, Any] = {"claimed_run_id": run_id, "updated": _iso(now)}
            if force:
                if chain.state is not ChainState.EXHAUSTED:
                    return False
                fields |= {"state": ChainState.PENDING, "next_attempt_at": ""}
            else:
                if chain.state is not ChainState.PENDING:
                    return False
                due = _parse_iso(chain.next_attempt_at)
                if due is None or due > now:
                    return False
            chains[key] = {**asdict(chain), **fields}
            self._save(chains)
        return True

    def transition(
        self,
        project: str,
        loop: str,
        trigger_key: str,
        *,
        to: ChainState | None,
        now: datetime,
        next_attempt_at: datetime | None | object = _UNSET,
        last_run_id: str | None = None,
        expect_claim: str | None = None,
    ) -> RetryChain | None:
        """Atomarer Zustandswechsel in einer einzigen Transaktion (eine Sperre).

        Liest die Kette, prüft den Übergang (:func:`check_transition`) und
        schreibt das Ergebnis — ohne Lücke, in der ein anderer Prozess dazwischen
        schreiben könnte. ``to=None`` = Zustand unverändert. ``next_attempt_at``:
        ``_UNSET`` (Default) = unverändert, ``None`` = löschen, ``datetime`` =
        setzen. ``last_run_id=None`` = unverändert. Der Claim wird immer
        freigegeben.

        ``expect_claim`` (Compare-and-Set): wenn gesetzt, muss ``claimed_run_id``
        genau diesem Wert entsprechen (``""`` = frei), sonst
        :class:`ClaimConflict` — so überschreibt niemand den Claim eines anderen
        Versuchs.

        None, wenn die Kette nicht existiert; :class:`InvalidTransition` bei
        verbotenen Übergängen (z. B. Zielzustand aus einem veralteten Zustand)."""
        key = _store_key(project, loop, trigger_key)
        with locked(self.root):
            chains = self._load()
            chain = self._parse(chains.get(key, {}))
            if chain is None:
                return None
            if expect_claim is not None and chain.claimed_run_id != expect_claim:
                raise ClaimConflict(f"Kette {key}: Claim {chain.claimed_run_id!r} statt {expect_claim!r}")
            target = chain.state if to is None else to
            if target is not chain.state:
                check_transition(CHAIN_TRANSITIONS, chain.state, target)
            next_at = chain.next_attempt_at
            if next_attempt_at is not _UNSET:
                next_at = _iso(cast(datetime, next_attempt_at)) if next_attempt_at is not None else ""
            updated = RetryChain(
                project=chain.project,
                loop=chain.loop,
                trigger_key=chain.trigger_key,
                state=target,
                next_attempt_at=next_at,
                last_run_id=last_run_id if last_run_id is not None else chain.last_run_id,
                claimed_run_id="",
                created=chain.created,
                updated=_iso(now),
            )
            chains[key] = asdict(updated)
            self._save(chains)
        return updated

    def release_claim(
        self,
        project: str,
        loop: str,
        trigger_key: str,
        run_id: str,
        now: datetime,
        *,
        to: ChainState | None = None,
        next_attempt_at: datetime | None | object = _UNSET,
    ) -> RetryChain | None:
        """Claim von ``run_id`` freigeben (Compare-and-Release, eine Transaktion).

        Hält ein anderer Run den Claim oder hat sich der Zustand parallel so
        geändert, dass ``to`` nicht mehr erlaubt ist, passiert nichts (None)."""
        try:
            return self.transition(
                project, loop, trigger_key, to=to, now=now, next_attempt_at=next_attempt_at, expect_claim=run_id
            )
        except InvalidTransition:
            return None

    def set_state(
        self, project: str, loop: str, trigger_key: str, state: ChainState, now: datetime
    ) -> RetryChain | None:
        """Zustand ändern ohne laufenden Versuch (``resolved``/``dismissed``).

        ``dismissed`` löscht ``next_attempt_at`` (UI *verwerfen*)."""
        return self.transition(
            project,
            loop,
            trigger_key,
            to=state,
            now=now,
            next_attempt_at=None if state is ChainState.DISMISSED else _UNSET,
        )

    def due(self, now: datetime) -> list[RetryChain]:
        """Fällige, unclaimed ``pending``-Ketten (``next_attempt_at <= now``)."""
        out = []
        for chain in self.all():
            if chain.state is not ChainState.PENDING or chain.is_claimed:
                continue
            due = _parse_iso(chain.next_attempt_at)
            if due is not None and due <= now:
                out.append(chain)
        return out


# ---------------------------------------------------------------- Policy-Auflösung


def policy_for(spec: dict, loop_name: str) -> RetryPolicy | None:
    """Wirksame Retry-Policy eines Loops (ADR 0036, Entscheidung 1).

    Loop-``retry`` ersetzt den Projekt-Block vollständig (kein Feld-Merge);
    ``max: 0`` auf Loop-Ebene schaltet eine Projekt-Policy für den Loop ab.
    None = kein Retry. Defekte Blöcke sind Spec-Fehler (specs.py validiert);
    hier wird tolerant geparst (None)."""
    loop = (spec.get("loops") or {}).get(loop_name) or {}
    raw = loop.get("retry") if "retry" in loop else spec.get("retry")
    if raw is None:
        return None
    return RetryPolicy.parse(raw, f"loop {loop_name}.retry", [])


def trigger_key(spec: dict, loop_name: str, trigger: str) -> str | None:
    """Kanonischer Trigger-Schlüssel (ADR 0036, Begriffe); None = keine Kette.

    - Cron: ``cron:<Schedule-Id>`` (Cron-Ausdruck — stabil über alle Ticks).
    - Issue: ``issue:<owner>/<repo>#<nummer>``.
    - PR: ``pr:<owner>/<repo>#<nummer>`` (Head-SHA ist Teil des Trigger-Strings,
      kein Teil des Schlüssels).
    - Events ohne Issue-/PR-/Schedule-Bezug: kein Schlüssel → keine Kette.

    Das Repo im Schlüssel (``repo`` des ersten ``on_issue``- bzw.
    ``on_pr``-Triggers, sonst Projekt-``github``) ist auch das Repo, aus dem
    ein Versuch Issue/PR neu lädt."""
    if trigger == "cron":
        cron_expr = next((str(t["cron"]) for t in _loop_triggers(spec, loop_name, "cron") if t.get("cron")), "")
        return f"cron:{cron_expr}" if cron_expr else None
    for regex, ttype, kind in ((_ISSUE_TRIGGER_RE, "on_issue", "issue"), (_PR_TRIGGER_RE, "on_pr", "pr")):
        m = regex.match(trigger or "")
        if m:
            repo = _trigger_repo(spec, loop_name, ttype)
            return f"{kind}:{repo}#{m.group(1)}" if repo else None
    return None


def key_for_record(spec: dict | None, loop_name: str, record: dict) -> str:
    """Ketten-Schlüssel eines Runs ("" = keine Kette).

    Das Record-Feld ``trigger_key`` (beim Run-Start geschrieben) gewinnt —
    eine Spec-Änderung während der Kette (Cron-Ausdruck, Repo) verwaist sie
    so nicht. Nur Alt-Records ohne Feld werden aus ``spec`` neu berechnet."""
    key = str(record.get("trigger_key") or "")
    if key or not spec:
        return key
    return trigger_key(spec, loop_name, str(record.get("trigger") or "")) or ""


def _loop_triggers(spec: dict, loop_name: str, ttype: str) -> list[dict]:
    """Trigger eines Typs eines Loops, in Spec-Reihenfolge."""
    loop = (spec.get("loops") or {}).get(loop_name) or {}
    return [t for t in loop.get("triggers") or [] if isinstance(t, dict) and t.get("type") == ttype]


def _trigger_repo(spec: dict, loop_name: str, ttype: str) -> str:
    """Repo des ersten ``ttype``-Triggers (Trigger-``repo`` vor Projekt-``github``)."""
    trigs = _loop_triggers(spec, loop_name, ttype)
    return str((trigs[0].get("repo") if trigs else None) or spec.get("github") or "")


def pr_accepts_closed(spec: dict, loop_name: str) -> bool:
    """Feuert der (schlüsselgebende, erste) ``on_pr``-Trigger auch auf
    geschlossene PRs (``states: [.., closed]``)? Dann beendet ein
    geschlossener/gemergter PR die Kette nicht."""
    trigs = _loop_triggers(spec, loop_name, "on_pr")
    return bool(trigs) and "closed" in {str(s) for s in trigs[0].get("states") or []}


# ---------------------------------------------------------------- Cron-Intervall


_cron_interval_cache: dict[str, timedelta | None] = {}
# Simulationsfenster: das Schaltjahr 2028 (der 29.2. liegt darin) plus eine
# Woche, damit Jahreswechsel und Wochentags-Zyklus mit abgedeckt sind.
_CRON_SIM_START = datetime(2028, 1, 1)
_CRON_SIM_DAYS = 366 + 7


def cron_min_interval(expr: str) -> timedelta | None:
    """Kürzester Abstand zweier Ticks eines Cron-Ausdrucks (gecacht).

    Gleiche Feld-Semantik wie :func:`fabrik.cron.matches`. Statt
    Minute für Minute zu simulieren, werden die Uhrzeiten eines Tages und die
    passenden Tage getrennt bestimmt: das Minimum ist der kleinste Abstand
    innerhalb eines Tages oder zwischen letztem Tick eines Tages und erstem
    Tick des nächsten passenden Tages. None, wenn das Cron ungültig ist oder im
    Fenster weniger als zwei Ticks liegen (z. B. nur der 29.2.) — dann wird die
    Intervall-Validierung übersprungen."""
    if expr not in _cron_interval_cache:
        parts = str(expr).split()
        interval = None
        if len(parts) == 5:
            # wie cron_matches: ungültig (auch Schritt 0) → kein Tick
            with contextlib.suppress(ValueError, IndexError, ZeroDivisionError):
                interval = _min_tick_gap(*parts)
        _cron_interval_cache[expr] = interval
    return _cron_interval_cache[expr]


def _min_tick_gap(f_min: str, f_hour: str, f_dom: str, f_mon: str, f_dow: str) -> timedelta | None:
    times = cron.day_times(f_min, f_hour)
    if not times:
        return None
    best = min((b - a for a, b in zip(times, times[1:], strict=False)), default=None)
    prev_last: datetime | None = None
    for i in range(_CRON_SIM_DAYS):
        day = _CRON_SIM_START + timedelta(days=i)
        if not cron.day_matches(f_dom, f_mon, f_dow, day):
            continue
        if prev_last is not None:
            gap = day + times[0] - prev_last
            best = gap if best is None else min(best, gap)
        prev_last = day + times[-1]
    return best if prev_last is not None else None


def validate_retry_cron(spec: dict, lname: str, ldef: dict, policy: RetryPolicy | None) -> tuple[list[str], list[str]]:
    """Cron-Intervall-Validierung (ADR 0036, Entscheidung 2).

    Eine tatsächlich genutzte Wartezeit (``delay(1..max)``) ≥ Intervall →
    Fehler (der Tick wäre früher als der Retry); Backoff-Werte jenseits von
    ``max`` werden nie benutzt und zählen nicht. Die ganze Kette (Summe der
    ersten ``max`` Wartezeiten) länger als das Intervall → Warnung (Ticks
    werden übersprungen)."""
    errs: list[str] = []
    warns: list[str] = []
    if policy is None:
        return errs, warns
    delays = [policy.delay(i) for i in range(1, policy.max + 1)]
    for trig in ldef.get("triggers") or []:
        if not isinstance(trig, dict) or trig.get("type") != "cron":
            continue
        interval = cron_min_interval(str(trig.get("cron") or ""))
        if interval is None:
            continue
        for value in sorted(set(delays)):
            if value >= interval:
                errs.append(f"loop {lname}: retry.backoff-Wert {value} >= Cron-Intervall {interval}")
        total = sum(delays, timedelta(0))
        if total > interval:
            warns.append(
                f"loop {lname}: Retry-Kette ({total}) länger als Cron-Intervall ({interval}) — Ticks werden übersprungen"
            )
    return errs, warns


# ---------------------------------------------------------------- Ketten-Updates


@dataclass(frozen=True, slots=True)
class RunEnd:
    """Die für die Kette relevanten Fakten eines beendeten (oder pausierten) Runs."""

    run_id: str
    status: str | None
    kind: FailureKind
    attempt: int = 0
    retry_of: str = ""
    finished: datetime | None = None
    retry_after: datetime | None = None

    @classmethod
    def from_record(cls, record: dict, now: datetime) -> RunEnd:
        return cls(
            run_id=str(record.get("id") or ""),
            status=record.get("status"),
            kind=parse_kind(record.get("failure_kind")),
            attempt=attempt_of(record),
            retry_of=str(record.get("retry_of") or ""),
            finished=_parse_iso(str(record.get("finished") or "")) or now,
            retry_after=parse_retry_after(record.get("retry_after")),
        )

    def belongs_to(self, chain: RetryChain | None) -> bool:
        """Ist der Run ein Versuch dieser Kette?

        Ja, wenn er den Claim hält — oder (ohne Claim, z. B. Altbestand) wenn
        er der Nachfolger des letzten Runs der Kette ist."""
        if chain is None:
            return False
        if chain.is_claimed:
            return chain.claimed_run_id == self.run_id
        return bool(self.retry_of) and self.retry_of == chain.last_run_id


class ChainAction(StrEnum):
    NONE = "none"  # nichts schreiben
    CREATE = "create"  # neue Kette
    TRANSITION = "transition"  # Zustandswechsel (+ Claim-Freigabe)


@dataclass(frozen=True, slots=True)
class ChainDecision:
    """Ergebnis von :func:`_decide_outcome`.

    ``target=None`` bei ``TRANSITION``: Zustand bleibt (nur Felder/Claim).
    ``next_attempt_at``: ``_UNSET`` = unverändert, ``None`` = löschen,
    ``datetime`` = setzen. ``attempt``: Versuchszahl für :func:`_chain_info`.
    ``in_chain=False``: der Run gehört zu keiner Kette — Alerts wie ohne Retry."""

    action: ChainAction
    target: ChainState | None = None
    next_attempt_at: datetime | None | object = _UNSET
    last_run_id: str | None = None
    attempt: int = 0
    in_chain: bool = True


_NOT_IN_CHAIN = ChainDecision(ChainAction.NONE, in_chain=False)


def _chain_info(chain: RetryChain | None, policy: RetryPolicy | None, decision: ChainDecision) -> dict[str, Any]:
    """Info für UI/Alerts: Kette vorhanden? Zustand, nächster Versuch, Budget."""
    if chain is None or not decision.in_chain:
        return {"in_chain": False}
    return {
        "in_chain": True,
        "state": chain.state.value,
        "next_attempt_at": chain.next_attempt_at,
        "attempt": decision.attempt,
        "max": policy.max if policy else 0,
    }


def _decide_outcome(chain: RetryChain | None, policy: RetryPolicy | None, run: RunEnd) -> ChainDecision:
    """Reine Ketten-Entscheidung am Ende eines Runs (ADR 0036, Entscheidung 4).

    - ``pass`` (gleich welcher Run) → ``resolved``.
    - Hält ein *anderer* Run den Claim, entscheidet nur dieser — nichts tun.
    - ``rejected`` eines Versuchs → ``dismissed``.
    - Noch nicht beendet (``needs_approval``): nichts tun, der Claim bleibt.
    - Fehlschlag eines Versuchs einer beendeten Kette (z. B. während des
      Versuchs verworfen): plant nichts mehr.
    - Fehlschlag eines fremden Runs, während die Kette ``pending`` ist (z. B.
      manueller Run im Backoff): die Kette plant unverändert weiter, das Budget
      bleibt.
    - Fehlschlag ohne Policy, mit Klasse außerhalb von ``on`` oder ``n >= max``:
      ein Versuch erschöpft die Kette; sonst keine Kette (klassisches
      ``run_failed``).
    - Sonst: ``n < max`` → ``pending`` mit Backoff (neue Kette, falls keine
      aktive existiert; eine ``exhausted``-Kette wird mit vollem Budget wieder
      geöffnet)."""
    belongs = run.belongs_to(chain)
    active = chain is not None and chain.is_active
    if run.status == RunStatus.PASS:
        return (
            ChainDecision(ChainAction.TRANSITION, ChainState.RESOLVED, attempt=run.attempt) if active else _NOT_IN_CHAIN
        )
    if chain is not None and chain.is_claimed and not belongs:
        return _NOT_IN_CHAIN
    if run.status == RunStatus.REJECTED:
        if belongs and active:
            return ChainDecision(
                ChainAction.TRANSITION, ChainState.DISMISSED, next_attempt_at=None, attempt=run.attempt
            )
        return _NOT_IN_CHAIN
    if run.status not in FAILED_RUN_STATUSES:
        return ChainDecision(ChainAction.NONE, attempt=run.attempt, in_chain=belongs)
    if belongs and not active:
        return ChainDecision(ChainAction.NONE, attempt=run.attempt)  # verworfen/gelöst: kein Alert
    if chain is not None and chain.state is ChainState.PENDING and not belongs:
        return _NOT_IN_CHAIN
    if policy is None or not policy.allows(run.kind) or run.attempt >= policy.max:
        if belongs:
            return ChainDecision(ChainAction.TRANSITION, ChainState.EXHAUSTED, None, run.run_id, attempt=run.attempt)
        return _NOT_IN_CHAIN
    next_at = policy.next_attempt_at(run.attempt + 1, run.finished or config.utcnow(), run.retry_after)
    action = ChainAction.TRANSITION if active else ChainAction.CREATE
    return ChainDecision(action, ChainState.PENDING, next_at, run.run_id, attempt=run.attempt + 1)


def on_run_finished(spec: dict, project: str, loop_name: str, record: dict) -> dict[str, Any]:
    """Ketten-Update am Ende eines Runs (ADR 0036, Entscheidung 4).

    Die Entscheidung (:func:`_decide_outcome`) ist rein; sie wird atomar
    angewendet (:meth:`ChainStore.transition` mit Compare-and-Set auf den
    gelesenen Claim / :meth:`ChainStore.create`). Ändert sich die Kette
    zwischen Lesen und Schreiben (paralleles Ende, neuer Claim), wird einmal
    neu entschieden — ein Konflikt bricht den Run nicht ab.

    Der Ketten-Schlüssel kommt aus dem Run-Record (:func:`key_for_record`).

    Liefert :func:`_chain_info` (``in_chain`` steuert u. a. die
    ``run_failed``-Alert-Dämpfung, ADR 0036 Entscheidung 6)."""
    key = key_for_record(spec, loop_name, record)
    if not key:
        return {"in_chain": False}  # kein kanonischer Trigger → keine Kette
    policy = policy_for(spec, loop_name)
    store = ChainStore()
    now = config.utcnow()
    run = RunEnd.from_record(record, now)

    for _ in range(2):  # ein Konflikt-Retry bei parallelem Zustandswechsel
        chain = store.get(project, loop_name, key)
        decision = _decide_outcome(chain, policy, run)
        try:
            if decision.action is ChainAction.CREATE:
                chain = store.create(
                    project,
                    loop_name,
                    key,
                    next_attempt_at=cast(datetime, decision.next_attempt_at),
                    last_run_id=cast(str, decision.last_run_id),
                    now=now,
                )
            elif decision.action is ChainAction.TRANSITION:
                chain = store.transition(
                    project,
                    loop_name,
                    key,
                    to=decision.target,
                    now=now,
                    next_attempt_at=decision.next_attempt_at,
                    last_run_id=decision.last_run_id,
                    expect_claim=chain.claimed_run_id if chain is not None else None,
                )
        except ChainStoreCorrupt:
            raise  # kein Konflikt: defekter Store → on_run_finished_safe meldet ihn
        except ValueError:  # pending-Kette parallel angelegt / InvalidTransition / ClaimConflict
            continue
        return _chain_info(chain, policy, decision)
    return {"in_chain": False}


def on_run_finished_safe(spec: dict, project: str, loop_name: str, record: dict) -> dict[str, Any]:
    """Best-effort :func:`on_run_finished` für die Run-Ende-Pfade (Engine,
    Stale-Run-Check, Reaper, Ablehnung): Store-Fehler (z. B. volles
    Dateisystem) werden geloggt, nicht propagiert — die Ketten-Buchhaltung
    darf das Alert des Runs nicht überspringen und den Server-Start nicht
    scheitern lassen."""
    try:
        return on_run_finished(spec, project, loop_name, record)
    except (OSError, ValueError) as e:
        print(f"[fabrik] Retry-Kette: Run-Ende {project}/{loop_name} nicht gebucht: {e}")
        return {"in_chain": False}


def record_run_end(project: str, record: dict, projects: Mapping[str, dict] | None = None) -> dict[str, Any]:
    """:func:`on_run_finished` für Aufrufer ohne Spec in der Hand (Reaper,
    Ablehnung am Approval-Gate). Unbekanntes Projekt → keine Kette.
    ``projects``: bereits geladene Specs (Aufrufer mit vielen Runs laden sie
    einmal statt je Run); sonst :func:`load_projects`.
    Best-effort (siehe :func:`on_run_finished_safe`)."""
    spec = (load_projects() if projects is None else projects).get(project)
    if not spec:
        return {"in_chain": False}
    return on_run_finished_safe(spec, project, str(record.get("loop") or ""), record)


# ---------------------------------------------------------------- Versuche starten


def _trigger_for_key(trigger_key: str, initial_vars: dict | None = None) -> str:
    """Trigger-String für einen Versuch aus dem kanonischen Schlüssel.

    PR-Versuche tragen den aktuellen Head-SHA (``on_pr:#N@<sha>``, ADR 0023)
    — der Scheduler reconciliert dagegen; ohne SHA würde ein Retry auf dem
    neuen Head einen zweiten Run auslösen. Der SHA kommt aus den (frisch
    geladenen) Trigger-Variablen des Versuchs."""
    m = _GH_KEY_RE.match(trigger_key)
    if m:
        kind, _repo, num = m.groups()
        if kind == "pr":
            from .scheduler import _pr_trigger  # lokal: scheduler importiert engine (Zyklus)

            return _pr_trigger(int(num), str((initial_vars or {}).get("pr_head_sha") or ""))
        return f"on_{kind}:#{num}"
    return "cron"


def _attempt_vars(trigger_key: str, *, accept_closed: bool = False) -> tuple[dict | None, bool]:
    """Frische Trigger-Variablen für einen Versuch (Issue-/PR-Inhalt kann sich
    geändert haben; ein Versuch durchläuft alle Gates neu, ADR 0036 Nr. 5).

    Liefert ``(vars, api_error)``: ``vars=None`` heißt, der Trigger gilt nicht
    mehr (Issue/PR weg oder geschlossen) — außer ``api_error=True``
    (transient, Kette bleibt pending). ``accept_closed`` (:func:`pr_accepts_closed`):
    ein geschlossener PR gilt weiter. Cron-Trigger haben keine Variablen:
    ``({}, False)``."""
    from .scheduler import issue_vars, pr_vars  # lokal: scheduler importiert engine (Zyklus)

    m = _GH_KEY_RE.match(trigger_key)
    if not m:
        return {}, False
    kind, repo, num = m.groups()
    data, api_error = _gh_get(repo, kind, num, accept_closed=accept_closed and kind == "pr")
    if data is None:
        return None, api_error
    return (issue_vars(data) if kind == "issue" else pr_vars(data)), False


def _dedupe_vars(trigger_key: str) -> dict | None:
    """Issue-/PR-Bezug eines Schlüssels für den Run-Claim (:func:`fabrik.runs.dedupe_key`),
    ohne GitHub-Abruf."""
    m = _GH_KEY_RE.match(trigger_key)
    if not m:
        return None
    kind, _repo, num = m.groups()
    return {f"{kind}_number": int(num)}


_GH_ENDPOINTS = {"issue": "issues", "pr": "pulls"}


def _gh_get(repo: str, kind: str, num: str, *, accept_closed: bool = False) -> tuple[dict | None, bool]:
    """Offenes Issue bzw. offenen PR ``num`` aus ``repo`` laden.

    Liefert ``(data, api_error)``:

    - ``(dict, False)``: vorhanden und offen (bzw. geschlossen bei ``accept_closed``);
    - ``(None, False)``: endgültig weg (HTTP 404, geschlossen, gemergt);
    - ``(None, True)``: API-Fehler oder kein Token (transient — Kette bleibt pending)."""
    from .gitops import _gh_api, _gh_token_safe

    label = f"{repo} {kind} #{num}"
    tok = _gh_token_safe()
    if not tok:
        return None, True
    try:
        data = _gh_api("GET", f"https://api.github.com/repos/{repo}/{_GH_ENDPOINTS[kind]}/{num}", tok)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, False
        print(f"[fabrik] Retry: {label} nicht abrufbar: {e}")
        return None, True
    except Exception as e:
        print(f"[fabrik] Retry: {label} nicht abrufbar: {e}")
        return None, True
    if not isinstance(data, dict) or "number" not in data:
        return None, False
    if data.get("state") == "closed" and not accept_closed:
        return None, False
    return data, False


@dataclass(frozen=True, slots=True)
class _Attempt:
    """Ein Versuch einer Kette, der den Ketten-Claim hält.

    Geplante Versuche laufen im Worker-Pool (:meth:`run`): kein Slot frei,
    Pause oder API-Fehler verschieben sie um :data:`ATTEMPT_START_DEFER`,
    ohne Budget zu verbrauchen. ``manual`` (UI *jetzt erneut*,
    :meth:`run_manual`) wartet wie andere manuelle Runs mit Vorrang auf einen
    Slot (ADR 0015); Pause blockiert nicht (nur ``disabled``). Kann er nicht
    starten, liegt die Kette wieder ``exhausted`` ab, und unter der bereits
    gemeldeten Run-Id steht ein ``error``-Record mit dem Grund.
    ``accept_closed``: siehe :func:`pr_accepts_closed`. ``actor``:
    auslösender Benutzer → ``started_by`` (Audit, ADR 0030). ``github_env_file``: Token-Datei
    des Projekts für das Nachladen der Trigger-Variablen (``project_github``)."""

    chain: RetryChain
    run_id: str
    attempt: int
    manual: bool = False
    accept_closed: bool = False
    actor: str | None = None
    github_env_file: str | None = None

    def blocked_reason(self) -> str | None:
        c = self.chain
        if self.manual:
            return pause.manual_block_reason(c.project, c.loop)
        state = pause.effective_state(c.project, c.loop)
        return None if state == pause.ACTIVE else state

    def abort(self, reason: str) -> None:
        """Versuch startet nicht: Claim freigeben, ohne Budget zu verbrauchen.

        No-Op auf der Kette, wenn der Claim uns nicht mehr gehört."""
        c = self.chain
        print(f"[fabrik] Retry-Versuch {self.run_id} ({c.key}) nicht gestartet: {reason}")
        now = config.utcnow()
        store = ChainStore()
        if self.manual:
            store.release_claim(
                c.project, c.loop, c.trigger_key, self.run_id, now, to=ChainState.EXHAUSTED, next_attempt_at=None
            )
            self._record_not_started(reason)
        else:
            store.release_claim(
                c.project, c.loop, c.trigger_key, self.run_id, now, next_attempt_at=now + ATTEMPT_START_DEFER
            )

    def dismiss(self) -> None:
        """Trigger gilt nicht mehr (Issue/PR weg oder geschlossen)."""
        c = self.chain
        ChainStore().release_claim(
            c.project,
            c.loop,
            c.trigger_key,
            self.run_id,
            config.utcnow(),
            to=ChainState.DISMISSED,
            next_attempt_at=None,
        )
        print(f"[fabrik] Retry-Kette {c.key}: Trigger nicht mehr vorhanden → dismissed")
        if self.manual:
            self._record_not_started("Trigger nicht mehr vorhanden")

    def _record_not_started(self, reason: str) -> None:
        """``error``-Record unter der Run-Id eines manuellen Versuchs, die die
        UI schon erhalten hat — sonst zeigte sie auf einen Run, den es nie gibt.
        Ein vorhandener Record (der Run hat begonnen) bleibt unangetastet."""
        c = self.chain
        if get_run(self.run_id, c.project) is not None:
            return
        now = config.utcnow().isoformat()
        record: dict[str, Any] = {
            "id": self.run_id,
            "project": c.project,
            "loop": c.loop,
            "trigger": _trigger_for_key(c.trigger_key),
            "note": "",
            "started": now,
            "finished": now,
            "status": RunStatus.ERROR.value,
            "error": f"Retry nicht gestartet: {reason}",
            "steps": [],
            "started_by": self.actor,
            "approved_by": None,
            "rejected_by": None,
            "attempt": self.attempt,
            "retry_of": c.last_run_id,
            "trigger_key": c.trigger_key,
        }
        try:
            STORE.create(c.project, self.run_id, record)
        except (OSError, RunStoreError) as e:
            print(f"[fabrik] Retry-Versuch {self.run_id}: Record nicht geschrieben: {e}")

    def _prepare(self) -> dict | None:
        """Gates prüfen, Trigger-Variablen laden, Claim-Besitz neu prüfen.

        Liefert die Trigger-Variablen; None, wenn der Versuch nicht startet
        (Kette bereits abgelegt/verschoben/verworfen)."""
        c = self.chain
        reason = self.blocked_reason()
        if reason:
            self.abort(reason)
            return None
        with project_github({SPEC_ENV_FILE_KEY: self.github_env_file}):
            initial_vars, api_error = _attempt_vars(c.trigger_key, accept_closed=self.accept_closed)
        if initial_vars is None:
            if api_error:
                self.abort("GitHub nicht erreichbar")
            else:
                self.dismiss()
            return None
        # Der Claim kann zwischen Claim und Start freigegeben worden sein
        # (verwerfen/Zustandswechsel geben ihn bedingungslos frei): die Kette
        # ist dann bereits beendet — nicht starten.
        fresh = ChainStore().get(c.project, c.loop, c.trigger_key)
        if fresh is None or fresh.claimed_run_id != self.run_id:
            self.abort("Claim nicht mehr gehalten")
            return None
        return initial_vars

    def _execute(self, scope: str, initial_vars: dict) -> None:
        """Loop mit gehaltenem Run-Slot ausführen. Der Record trägt den
        Schlüssel dieser Kette (``chain_key``), nicht einen aus der
        (inzwischen evtl. geänderten) Spec berechneten."""
        from .engine import run_loop

        c = self.chain
        try:
            run_loop(
                c.project,
                c.loop,
                trigger=_trigger_for_key(c.trigger_key, initial_vars),
                initial_vars=initial_vars,
                run_id=self.run_id,
                attempt=self.attempt,
                retry_of=c.last_run_id,
                held_scope=scope,
                actor=self.actor,
                chain_key=c.trigger_key,
            )
        except Exception as e:
            print(f"[fabrik] Retry-Versuch {self.run_id} fehlgeschlagen: {e}")
            if not self._finish_crashed(e):
                self.abort(f"Start fehlgeschlagen: {e}")

    def _finish_crashed(self, exc: Exception) -> bool:
        """``run_loop`` brach ab, nachdem der Record angelegt war (z. B.
        ``_persist`` scheitert): Record als ``error`` beenden und die Kette
        buchen — der Versuch verbraucht Budget, sonst liefe ein dauerhafter
        Absturz endlos jede Minute. False, wenn es keinen Record gibt (der
        Versuch hat nicht begonnen)."""
        from .failures import classify_exception

        c = self.chain
        record = get_run(self.run_id, c.project)
        if record is None:
            return False
        if record.get("status") == RunStatus.RUNNING:
            record.update(
                status=RunStatus.ERROR.value,
                error=str(exc),
                finished=config.utcnow().isoformat(),
                **classify_exception(exc).fields(),
            )
            try:
                STORE.write(c.project, self.run_id, record, expect=RunStatus.RUNNING)
            except (OSError, RunStoreError) as e:
                print(f"[fabrik] Retry-Versuch {self.run_id}: Record nicht geschrieben: {e}")
        record_run_end(c.project, record)
        return True

    def run(self) -> None:
        """Geplanter Versuch (Worker-Pool): vorbereiten, Run-Slot nicht
        blockierend claimen, Loop ausführen."""
        from .runs import claim_run

        initial_vars = self._prepare()
        if initial_vars is None:
            return
        claim = claim_run(self.chain.project, self.chain.loop, initial_vars)
        if claim is None:
            self.abort("kein Run-Slot frei")
            return
        try:
            self._execute(claim.scope, initial_vars)
        finally:
            claim.release()

    def run_manual(self, held: RunClaim) -> None:
        """Manueller Versuch im Vorrang-Worker (:func:`fabrik.engine.spawn_priority_worker`),
        der den Run-Slot ``held`` bereits hält und danach freigibt."""
        initial_vars = self._prepare()
        if initial_vars is not None:
            self._execute(held.scope, initial_vars)


def _submit(attempt: _Attempt) -> None:
    from .engine import _get_run_pool  # lokal: engine importiert dieses Modul

    _get_run_pool().submit(attempt.run)


def _spawn_manual(attempt: _Attempt) -> None:
    """Manuellen Versuch mit Vorrang starten: sofort, oder er wartet auf den
    Slot (wie manuelle Runs, ADR 0015). RuntimeError, wenn der Thread nicht startet."""
    from .engine import spawn_priority_worker  # lokal: engine importiert dieses Modul

    c = attempt.chain
    spawn_priority_worker(c.project, c.loop, _dedupe_vars(c.trigger_key), work=attempt.run_manual)


def _end_chain(store: ChainStore, chain: RetryChain, to: ChainState, now: datetime, why: str) -> None:
    """Unclaimed Kette beenden (parallel weiterentwickelt → nichts tun)."""
    with contextlib.suppress(InvalidTransition):
        store.transition(
            chain.project, chain.loop, chain.trigger_key, to=to, now=now, next_attempt_at=None, expect_claim=""
        )
        print(f"[fabrik] Retry-Kette {chain.key}: {why} → {to}")


def start_due_attempts(projects: dict[str, dict] | None = None) -> list[str]:
    """Fällige Versuche starten (ADR 0036, Entscheidung 5) — auch nach Neustart.

    Je Kette genau ein Starter (der Claim gelingt für genau einen). Pausierte/
    deaktivierte Loops warten bis zur Freigabe und verbrauchen kein Budget.
    Ein gelöschter Loop beendet die Kette als ``dismissed``; ist Retry für den
    Loop inzwischen abgeschaltet, wird sie ``exhausted`` abgelegt. Ob Issue/PR
    noch gelten, prüft der Versuch im Worker (:meth:`_Attempt.run`) — der
    Scheduler-Tick macht keine GitHub-Abrufe. ``projects``: bereits geladene
    Specs (Scheduler-Tick); None = selbst laden. Gibt die gestarteten Run-Ids
    zurück."""
    store = ChainStore()
    now = config.utcnow()
    started: list[str] = []
    due = store.due(now)
    if not due:
        return started
    if projects is None:
        projects = load_projects()
    for chain in due:
        spec = projects.get(chain.project)
        if spec is None or spec.get("_error"):
            continue  # Projekt fehlt/defekt: Kette bleibt pending (nächster Tick)
        if chain.loop not in (spec.get("loops") or {}):
            _end_chain(store, chain, ChainState.DISMISSED, now, "Loop nicht mehr vorhanden")
            continue
        if policy_for(spec, chain.loop) is None:
            _end_chain(store, chain, ChainState.EXHAUSTED, now, "Retry für den Loop abgeschaltet")
            continue
        if pause.trigger_blocked(chain.project, chain.loop):
            continue  # pausiert/deaktiviert: wartet, kein Budget-Verbrauch
        # Versuchszahl aus dem letzten Run ableiten (ADR 0036, Entscheidung 3).
        last = get_run(chain.last_run_id, chain.project) if chain.last_run_id else None
        attempt = attempt_of(last) + 1
        run_id = f"{config.now_id()}-{chain.loop}"
        if not store.claim(chain.project, chain.loop, chain.trigger_key, run_id, now):
            continue  # ein anderer Starter hat den Versuch bekommen
        started.append(run_id)
        print(f"[fabrik] Retry-Versuch {attempt} für {chain.key} (Run {run_id})")
        _submit(
            _Attempt(
                chain,
                run_id,
                attempt,
                accept_closed=pr_accepts_closed(spec, chain.loop),
                github_env_file=spec.get(SPEC_ENV_FILE_KEY),
            )
        )
    return started


def release_stale_claims() -> int:
    """Neustart: Claims von nicht mehr laufenden Runs freigeben (ADR 0036,
    Entscheidung 3). Der Reaper (ADR 0035) hat die Runs zuvor als
    ``interrupted`` beendet und die Kette aktualisiert; übrig bleiben Claims,
    deren Run fehlt oder deren Ende die Kette nie erreicht hat. Ein beendeter
    Run wird nachgetragen (:func:`record_run_end`), sonst wird der Claim nur
    freigegeben. Gibt die Anzahl freigegebener Claims zurück."""
    store = ChainStore()
    now = config.utcnow()
    freed = 0
    for chain in store.all():
        if not chain.is_claimed:
            continue
        run = get_run(chain.claimed_run_id, chain.project)
        status = run.get("status") if run is not None else None
        if status in (RunStatus.RUNNING, RunStatus.NEEDS_APPROVAL):
            continue  # Versuch läuft noch
        if run is not None and status in (*FAILED_RUN_STATUSES, RunStatus.PASS, RunStatus.REJECTED):
            record_run_end(chain.project, run)
        if chain.next_attempt_at == "":
            # Erzwungener Claim (jetzt erneut): der Versuch hat nie einen
            # Run-Record geschrieben → zurück nach exhausted. Eine
            # pending-Kette ohne next_attempt_at wäre nie wieder fällig
            # (Cron-Loop tot).
            store.release_claim(
                chain.project,
                chain.loop,
                chain.trigger_key,
                chain.claimed_run_id,
                now,
                to=ChainState.EXHAUSTED,
                next_attempt_at=None,
            )
        else:
            store.release_claim(chain.project, chain.loop, chain.trigger_key, chain.claimed_run_id, now)
        freed += 1
        print(f"[fabrik] Retry-Kette {chain.key}: Claim {chain.claimed_run_id} freigegeben (Run nicht aktiv)")
    return freed


# ---------------------------------------------------------------- Bestandsabgleich


@dataclass(slots=True)
class BackfillSummary:
    """Ergebnis von :func:`backfill_chains` (Zählungen je Wirkung)."""

    pending: int = 0
    exhausted: int = 0
    skipped: int = 0


def backfill_chains(
    projects: dict[str, dict] | None = None,
    store: ChainStore | None = None,
    now: datetime | None = None,
) -> BackfillSummary:
    """Einmaliger Bestandsabgleich (#290): Ketten für komplett gescheiterte Trigger.

    Für jeden Loop mit Retry-Policy werden die Run-Records nach kanonischen
    Trigger-Schlüsseln (:func:`key_for_record`) gruppiert. Eine Gruppe, in der
    **jeder** Run ``fail``/``error`` ist und für die noch keine Kette existiert,
    bekommt eine:

    - Klasse des letzten Runs (nach ``started``) liegt in ``on`` der Policy des
      Loops → ``pending``, fällig nach dem Backoff vor Versuch 1, gemessen ab
      Ende des letzten Runs (nicht vor ``now`` — der Versuch startet beim
      nächsten Scheduler-Tick, auch überfällige sofort); ein gemeldetes
      ``retry_after`` des letzten Runs verschiebt die Fälligkeit, auf
      ``max_wait`` gekappt (gleiche Semantik wie :meth:`RetryPolicy.next_attempt_at`);
    - jede andere Klasse (``environment``/``outcome``/``unknown``/ohne) →
      ``exhausted`` — landet in der UI-Liste „Abgelegt“ zur Durchsicht.

    Die gescheiterten Altruns zählen nicht gegen das Budget: der erste Versuch
    der Kette ist Versuch 1 (``attempt`` des letzten Runs + 1; Altruns tragen
    0 oder kein Feld), also volles Budget von ``max`` weiteren Versuchen. Ob
    der Trigger noch existiert (Issue geschlossen, PR gemergt), prüft der
    Abgleich nicht — der Versuch fragt GitHub beim ersten Tick ab und beendet
    die Kette als ``dismissed``, wenn der Trigger weg ist.

    Idempotent: vorhandene Ketten (jeder Zustand), Schlüssel ohne Kette (z. B.
    ``manual``) und Gruppen mit einem nicht-fehlgeschlagenen Run (``pass``,
    ``rejected``, ``queued``, …) werden übersprungen — laufende oder verworfene
    Ketten berührt der Abgleich nicht. ``projects``/``store``/``now`` sind für
    Tests überschreibbar; Default lädt Specs/Store/Zeit aus der Umgebung."""
    if projects is None:
        projects = load_projects()
    if store is None:
        store = ChainStore()
    if now is None:
        now = config.utcnow()
    summary = BackfillSummary()
    for name, spec in projects.items():
        if spec.get("_error"):
            continue
        # Wirksame Policy je Loop (Loop-``retry`` ersetzt den Projekt-Block) —
        # einmal auflösen, dann behalten (None = kein Retry).
        policies = {lname: pol for lname in spec.get("loops") or {} if (pol := policy_for(spec, lname)) is not None}
        if not policies:
            continue
        groups: dict[str, dict[str, list[dict]]] = {lname: {} for lname in policies}
        for r in load_records(name):
            groups_by_key = groups.get(str(r.get("loop") or ""))
            if groups_by_key is None:
                continue
            key = key_for_record(spec, str(r.get("loop") or ""), r)
            if key:
                groups_by_key.setdefault(key, []).append(r)
        for lname, groups_by_key in groups.items():
            policy = policies[lname]
            for key, group in groups_by_key.items():
                if store.get(name, lname, key) is not None:
                    summary.skipped += 1
                    continue
                if any(str(r.get("status")) not in FAILED_RUN_STATUSES for r in group):
                    summary.skipped += 1
                    continue
                last = max(group, key=lambda r: str(r.get("started") or ""))
                # Wirksame Policy des Loops (nicht die globalen Retry-Klassen):
                # ein `on`-Subset darf im Abgleich nicht aufgeweitet werden.
                kind = parse_kind(last.get("failure_kind"))
                if policy.allows(kind):
                    finished = _parse_iso(str(last.get("finished") or "")) or now
                    due = policy.next_attempt_at(1, finished, parse_retry_after(last.get("retry_after")))
                    store.create(
                        name, lname, key, next_attempt_at=max(due, now), last_run_id=str(last.get("id") or ""), now=now
                    )
                    summary.pending += 1
                    print(f"[fabrik] Bestandsabgleich {name}/{lname} {key}: Kette pending (Klasse {kind.value})")
                else:
                    store.create(
                        name,
                        lname,
                        key,
                        next_attempt_at=None,
                        last_run_id=str(last.get("id") or ""),
                        now=now,
                        state=ChainState.EXHAUSTED,
                    )
                    summary.exhausted += 1
                    print(
                        f"[fabrik] Bestandsabgleich {name}/{lname} {key}: Kette exhausted "
                        f"(Klasse {kind.value if kind else 'ohne'})"
                    )
    return summary


# ---------------------------------------------------------------- UI-Aktionen


def dismiss_chain(project: str, loop: str, trigger_key: str, actor: str = "unknown") -> RetryChain | None:
    """UI *verwerfen* (aus ``pending`` oder ``exhausted``): Kette ``dismissed``,
    ``next_attempt_at`` gelöscht. Ein laufender Versuch läuft zu Ende, plant
    aber nichts mehr (ADR 0036, Entscheidung 4) — :meth:`RunEnd.belongs_to`
    erkennt ihn an ``retry_of``."""
    store = ChainStore()
    chain = store.get(project, loop, trigger_key)
    if chain is None or not chain.is_active:
        return None
    try:
        updated = store.set_state(project, loop, trigger_key, ChainState.DISMISSED, config.utcnow())
    except InvalidTransition:
        return store.get(project, loop, trigger_key)  # parallel beendet
    print(f"[fabrik] Retry-Kette {chain.key} verworfen ({actor})")
    return updated


def retry_now(project: str, loop: str, trigger_key: str, actor: str | None = None) -> str | None:
    """UI *jetzt erneut* (nur aus ``exhausted``): Run mit ``attempt = 0``,
    ``retry_of = last_run_id``; Kette wieder ``pending`` mit vollem Budget
    (ADR 0036, Entscheidung 4). Ist kein Slot frei, wartet der Run mit
    Vorrang (ADR 0015). Der Run hält den Claim, bis er endet; kann er nicht
    starten, liegt die Kette wieder ``exhausted`` ab und die Run-Id zeigt auf
    einen ``error``-Record mit dem Grund. ``actor``: Benutzername →
    ``started_by`` (Audit, ADR 0030).

    Gibt die Run-Id zurück; None, wenn die Kette fehlt oder nicht ``exhausted``
    ist. :class:`RetryBlocked`, wenn der Loop deaktiviert ist oder der Worker
    nicht startet."""
    store = ChainStore()
    chain = store.get(project, loop, trigger_key)
    if chain is None or chain.state is not ChainState.EXHAUSTED:
        return None
    reason = pause.manual_block_reason(project, loop)
    if reason:
        raise RetryBlocked(reason)
    run_id = f"{config.now_id()}-{loop}"
    if not store.claim(project, loop, trigger_key, run_id, config.utcnow(), force=True):
        return None
    print(f"[fabrik] Retry-Kette {chain.key}: jetzt erneut ({actor or 'api'}, Run {run_id})")
    spec = load_projects().get(project) or {}
    attempt = _Attempt(
        chain,
        run_id,
        0,
        manual=True,
        accept_closed=pr_accepts_closed(spec, loop),
        actor=actor,
        github_env_file=spec.get(SPEC_ENV_FILE_KEY),
    )
    try:
        _spawn_manual(attempt)
    except RuntimeError as e:
        attempt.abort(f"Worker-Start fehlgeschlagen: {e}")
        raise RetryBlocked(f"Worker-Start fehlgeschlagen: {e}") from e
    return run_id
