"""Benannte Verbindungen (ADR 0052): LLM-Provider und Agent-Backends, in der UI verwaltet.

Eine :class:`Connection` bündelt Treiber, Endpoint, Modell und Zugang unter einem Namen. Sie liegt in
der Tabelle ``connections`` des SQL-Stores (Secret Fernet-verschlüsselt, Schlüssel aus
``FABRIK_SECRET_KEY`` oder ``state/secret.key``). Env-Konfiguration (``FABRIK_LLM_*``,
``FABRIK_AGENT_*``) bleibt gültig und erscheint als schreibgeschützte Verbindung; bei Namensgleichheit
gewinnt die UI-Verbindung. Aufgelöst wird vor dem Dispatch: :func:`apply_to_step` trägt die
Verbindungswerte in einen Agent-Step ein, sodass die Backends unverändert bleiben.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import RowMapping, delete, insert, select, update

from . import config, secrets
from .runschema import connections as connections_table

log = logging.getLogger("fabrik.connections")

SECRET_KEY_ENV = "FABRIK_SECRET_KEY"
SECRET_KEY_FILE = "secret.key"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
ENV_AGENT_NAME = "env"
DRIVER_HTTP = "http"  # LLM: OpenAI-kompatibler Endpoint (Gegenstück zu den CLI-Treibern)
AGENT_STEP_TYPES = frozenset({"agent", "review", "pr_review"})


class InvalidConnection(ValueError):
    """Ungültige oder unbekannte Verbindung."""


class UndecryptableSecret(InvalidConnection):
    """Gespeichertes Secret ist mit dem aktuellen Schlüssel nicht entschlüsselbar."""


class ConnectionKind(StrEnum):
    LLM = "llm"
    AGENT = "agent"


class Source(StrEnum):
    DB = "db"
    ENV = "env"


@dataclass(frozen=True)
class Connection:
    """Eine benannte Verbindung. ``driver``: Agent → Backend-Name; LLM → ``http`` oder CLI-Name."""

    name: str
    kind: ConnectionKind
    driver: str = ""
    endpoint: str = ""
    model: str = ""
    username: str = ""
    secret: str = field(default="", repr=False)
    is_default: bool = False
    source: Source = Source.DB
    secret_unreadable: bool = False  # gespeichert, aber nicht entschlüsselbar (Schlüssel gewechselt/verloren)

    def validate(self) -> None:
        """Pflichtfelder je Art/Treiber prüfen; wirft :class:`InvalidConnection`."""
        from . import llm_cli
        from .backends.registry import known_backends

        if not NAME_RE.match(self.name):
            raise InvalidConnection("Name: 1-48 Zeichen aus Buchstaben, Ziffern, '.', '_', '-' (nicht am Anfang)")
        if self.kind is ConnectionKind.AGENT:
            if self.driver not in known_backends():
                raise InvalidConnection(f"Backend '{self.driver}' unbekannt (verfügbar: {', '.join(known_backends())})")
            if self.driver == "opencode" and not (self.endpoint and self.model):
                raise InvalidConnection("opencode braucht endpoint und model")
        elif self.driver in llm_cli.CLIS:
            if not self.model:
                raise InvalidConnection("model fehlt")
        elif self.driver == DRIVER_HTTP:
            if not (self.endpoint and self.model):
                raise InvalidConnection("http-Provider brauchen endpoint und model")
        else:
            raise InvalidConnection(f"LLM-Treiber '{self.driver}' unbekannt (http, {', '.join(llm_cli.CLIS)})")

    def public(self) -> dict[str, Any]:
        """API-Form ohne Secret (``secret_set`` zeigt nur, OB eines gesetzt ist)."""
        return {
            "name": self.name,
            "kind": self.kind.value,
            "driver": self.driver,
            "endpoint": self.endpoint,
            "model": self.model,
            "username": self.username,
            "secret_set": bool(self.secret) or self.secret_unreadable,
            "secret_unreadable": self.secret_unreadable,
            "is_default": self.is_default,
            "source": self.source.value,
        }

    def as_llm_provider(self) -> dict[str, str]:
        """Provider-Dict im Format von :mod:`fabrik.llm` (``cli`` oder ``url``)."""
        if self.driver == DRIVER_HTTP:
            return {"name": self.name, "url": self.endpoint, "model": self.model, "key": self.secret}
        return {"name": self.name, "url": "", "cli": self.driver, "model": self.model, "key": ""}

    @classmethod
    def from_payload(cls, name: str, kind: str, data: Mapping[str, Any]) -> Connection:
        """API-Payload → Verbindung (ohne Secret-Semantik; siehe :func:`save_connection`)."""
        try:
            parsed_kind = ConnectionKind(kind)
        except ValueError:
            raise InvalidConnection(f"kind muss 'llm' oder 'agent' sein, nicht '{kind}'") from None

        def text(key: str) -> str:
            return str(data.get(key) or "").strip()

        return cls(
            name=name,
            kind=parsed_kind,
            driver=text("driver"),
            endpoint=text("endpoint"),
            model=text("model"),
            username=text("username"),
            secret=str(data.get("secret") or "").strip(),
            is_default=bool(data.get("is_default")),
        )


# ---------------------------------------------------------------- Verschlüsselung
_fernet_guard = threading.Lock()


def _key_file() -> Path:
    return config.STATE_DIR / SECRET_KEY_FILE


def _create_key_file(path: Path) -> None:
    """Key-File atomar anlegen: erst vollständig in eine Temp-Datei (0600) schreiben, dann per
    ``os.link`` veröffentlichen. So sieht kein paralleler Prozess eine leere Datei, und ein
    gleichzeitiger Gewinner (``FileExistsError``) ist kein Fehler."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
        with contextlib.suppress(FileExistsError):  # anderer Prozess war schneller — dessen Schlüssel gilt
            os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _load_key() -> bytes:
    """Fernet-Schlüssel: ``FABRIK_SECRET_KEY``, sonst Key-File (beim ersten Bedarf mit 0600 erzeugt)."""
    env = os.environ.get(SECRET_KEY_ENV, "").strip()
    if env:
        return env.encode()
    path = _key_file()
    with _fernet_guard:
        if not path.exists():
            _create_key_file(path)
        return path.read_bytes().strip()


def encrypt(plain: str) -> str:
    """Klartext → Fernet-Token ('' bleibt '')."""
    return Fernet(_load_key()).encrypt(plain.encode()).decode() if plain else ""


def decrypt(token: str) -> str:
    """Fernet-Token → Klartext. Nicht entschlüsselbar (Schlüssel gewechselt) → :class:`UndecryptableSecret`."""
    if not token:
        return ""
    try:
        return Fernet(_load_key()).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError) as e:
        raise UndecryptableSecret(
            f"Secret nicht entschlüsselbar (falscher {SECRET_KEY_ENV} / {SECRET_KEY_FILE}?) — neu eintragen"
        ) from e


# ---------------------------------------------------------------- Persistenz
def _store():
    from .runstore_sql import store_for_url  # lokal: SQLAlchemy nur laden, wenn genutzt

    return store_for_url(config.db_url())


_secret_cache: list[str] | None = None
_secret_cache_at = 0.0  # monotonic; Zeitpunkt des letzten erfolgreichen Ladens
_secret_cache_failed_at: float | None = None  # monotonic; gesetzt, wenn das Laden zuletzt scheiterte
SECRET_CACHE_TTL_S = 30.0  # andere fabrik-Prozesse auf derselben DB sehen Änderungen spätestens dann
SECRET_RETRY_S = 30.0  # nach einem Fehler so lange nicht erneut die DB fragen (Masking läuft pro Log-Zeile)


def _invalidate() -> None:
    global _secret_cache, _secret_cache_failed_at
    _secret_cache = None
    _secret_cache_failed_at = None


def _from_row(row: RowMapping, *, with_secret: bool = True) -> Connection:
    """DB-Zeile → Verbindung. Ein nicht entschlüsselbares Secret bricht Listen nicht ab, sondern wird
    als ``secret_unreadable`` markiert (``apply_to_step`` weist es dann mit klarer Meldung ab).
    ``with_secret=False`` entschlüsselt gar nicht (Secret leer) — für reine Anzeige."""
    secret, unreadable = "", False
    try:
        secret = decrypt(row["secret"]) if with_secret else ""
    except UndecryptableSecret:
        unreadable = True
        log.warning(
            "Connection '%s': Secret nicht entschlüsselbar (%s / %s?)", row["name"], SECRET_KEY_ENV, SECRET_KEY_FILE
        )
    return Connection(
        name=row["name"],
        kind=ConnectionKind(row["kind"]),
        driver=row["driver"],
        endpoint=row["endpoint"],
        model=row["model"],
        username=row["username"],
        secret=secret,
        is_default=bool(row["is_default"]),
        secret_unreadable=unreadable,
    )


def db_connections(kind: ConnectionKind | None = None, *, with_secret: bool = True) -> list[Connection]:
    """UI-verwaltete Verbindungen (Secret entschlüsselt, außer ``with_secret=False``), nach Name sortiert."""
    q = select(connections_table).order_by(connections_table.c.name)
    if kind is not None:
        q = q.where(connections_table.c.kind == kind.value)
    with _store().read_tx() as conn:
        return [_from_row(r, with_secret=with_secret) for r in conn.execute(q).mappings()]


def env_connections() -> list[Connection]:
    """Schreibgeschützte Verbindungen aus der Env-Konfiguration (Agent ``env`` + LLM-Provider)."""
    from . import llm

    out = [
        Connection(
            name=p["name"],
            kind=ConnectionKind.LLM,
            driver=p.get("cli") or DRIVER_HTTP,
            endpoint=p.get("url", ""),
            model=p["model"],
            secret=p.get("key", ""),
            source=Source.ENV,
        )
        for p in llm.env_providers()
    ]
    backend = os.environ.get("FABRIK_AGENT_BACKEND", "").strip()
    if backend:
        out.append(
            Connection(
                name=ENV_AGENT_NAME,
                kind=ConnectionKind.AGENT,
                driver=backend,
                endpoint=os.environ.get("FABRIK_AGENT_ENDPOINT", "").strip(),
                model=os.environ.get("FABRIK_AGENT_MODEL", "").strip(),
                username=os.environ.get("FABRIK_AGENT_USERNAME", "").strip(),
                secret=os.environ.get("FABRIK_AGENT_PASSWORD", "").strip(),
                source=Source.ENV,
            )
        )
    return out


def list_connections(kind: ConnectionKind | None = None, *, with_secret: bool = True) -> list[Connection]:
    """Alle Verbindungen: Env zuerst, UI-Verbindungen ersetzen gleichnamige Env-Einträge."""
    merged: dict[tuple[ConnectionKind, str], Connection] = {}
    for c in env_connections() + db_connections(with_secret=with_secret):
        if kind is None or c.kind is kind:
            merged[(c.kind, c.name)] = c
    return list(merged.values())


def get_connection(name: str, kind: ConnectionKind, *, with_secret: bool = True) -> Connection | None:
    return next((c for c in list_connections(kind, with_secret=with_secret) if c.name == name), None)


def default_connection(kind: ConnectionKind, *, with_secret: bool = True) -> Connection | None:
    """Als Default markierte UI-Verbindung der Art (``None`` → Env-/Legacy-Konfiguration gilt)."""
    return next((c for c in db_connections(kind, with_secret=with_secret) if c.is_default), None)


def save_connection(conn: Connection, *, keep_secret: bool = False) -> Connection:
    """Verbindung anlegen/ersetzen (nach Validierung). ``keep_secret``: bestehendes Secret behalten.

    Ein Default pro Art: ``is_default`` löscht die Markierung bei den anderen."""
    conn.validate()
    values = {
        "kind": conn.kind.value,
        "driver": conn.driver,
        "endpoint": conn.endpoint,
        "model": conn.model,
        "username": conn.username,
        "is_default": conn.is_default,
        "updated_at": config.utcnow().isoformat(),
    }
    t = connections_table
    with _store().write_tx() as db:
        existing = db.execute(select(t.c.kind, t.c.secret).where(t.c.name == conn.name)).mappings().first()
        if existing and existing["kind"] != conn.kind.value:
            raise InvalidConnection(f"'{conn.name}' existiert bereits als {existing['kind']}-Verbindung")
        values["secret"] = existing["secret"] if (keep_secret and existing) else encrypt(conn.secret)
        if conn.is_default:
            db.execute(update(t).where(t.c.kind == conn.kind.value).values(is_default=False))
        if existing:
            db.execute(update(t).where(t.c.name == conn.name).values(**values))
        else:
            db.execute(insert(t).values(name=conn.name, **values))
    _invalidate()
    saved = next(c for c in db_connections(conn.kind) if c.name == conn.name)
    return replace(saved)


def delete_connection(name: str) -> bool:
    """UI-Verbindung löschen; ``False`` = gab es nicht (Env-Verbindungen sind nicht löschbar)."""
    with _store().write_tx() as db:
        deleted = db.execute(delete(connections_table).where(connections_table.c.name == name)).rowcount
    _invalidate()
    return bool(deleted)


def secret_values() -> list[str]:
    """Entschlüsselte Secrets aller UI-Verbindungen — für das Log-/Record-Masking (:data:`SECRET_CACHE_TTL_S` gecacht).

    Fehler (DB/Schlüssel) brechen den Aufrufer nie; sie werden negativ gecacht (:data:`SECRET_RETRY_S`)
    und einmal je Fenster geloggt, damit nicht jede Log-Zeile die DB anfragt."""
    global _secret_cache, _secret_cache_at, _secret_cache_failed_at
    now = time.monotonic()
    if _secret_cache is not None and now - _secret_cache_at < SECRET_CACHE_TTL_S:
        return _secret_cache
    if _secret_cache_failed_at is not None and now - _secret_cache_failed_at < SECRET_RETRY_S:
        return []
    try:
        _secret_cache = [c.secret for c in db_connections() if len(c.secret) >= secrets.MIN_SECRET_LEN]
        _secret_cache_at = now
    except Exception:
        _secret_cache_failed_at = now
        log.warning("Connection-Secrets fürs Masking nicht ladbar — Masking lückenhaft", exc_info=True)
        return []
    return _secret_cache


secrets.register_source(secret_values)


def unknown_references(spec: Mapping[str, Any]) -> list[str]:
    """Agent-Steps der Spec, deren ``connection:`` es nicht gibt (für ``fabrik validate``).

    Host-abhängig (DB + Env dieses Hosts) — daher Warnung, kein Spec-Fehler. Werte mit
    Platzhaltern (``{var}``) werden erst zur Laufzeit aufgelöst und nicht geprüft."""
    refs = [
        (loop_name, str(step.get("name") or "?"), str(step["connection"]).strip())
        for loop_name, loop in (spec.get("loops") or {}).items()
        for step in (loop.get("steps") or [])
        if isinstance(step, dict) and step.get("type") in AGENT_STEP_TYPES and step.get("connection")
    ]
    refs = [r for r in refs if "{" not in r[2]]
    if not refs:
        return []
    known = {c.name for c in list_connections(ConnectionKind.AGENT, with_secret=False)}
    return [f"{loop}/{step}: Agent-Verbindung '{name}' unbekannt" for loop, step, name in refs if name not in known]


# ---------------------------------------------------------------- Auflösung im Step
_STEP_FIELDS = (("backend", "driver"), ("endpoint", "endpoint"), ("model", "model"), ("username", "username"))


def apply_to_step(step: dict, *, with_secret: bool = True) -> dict:
    """Agent-Step um Verbindungswerte ergänzen (Step-Felder gewinnen, Env bleibt Fallback).

    ``connection: <name>`` im Step wählt explizit (unbekannt oder abweichendes ``backend`` → Fehler);
    ohne Angabe greift die Default-Verbindung, sofern ihr Backend zum Step passt.
    ``with_secret=False``: nur Anzeigefelder (kein Entschlüsseln, kein ``password``) — z. B. für den Step-Intent."""
    if step.get("type") not in AGENT_STEP_TYPES:
        return step
    name = str(step.get("connection") or "").strip()
    if name:
        conn = get_connection(name, ConnectionKind.AGENT, with_secret=with_secret)
        if conn is None:
            raise InvalidConnection(f"Agent-Verbindung '{name}' unbekannt")
        if step.get("backend") and step["backend"] != conn.driver:
            raise InvalidConnection(f"backend '{step['backend']}' passt nicht zur Verbindung '{name}' ({conn.driver})")
    else:
        conn = default_connection(ConnectionKind.AGENT, with_secret=with_secret)
        if conn is None or (step.get("backend") and step["backend"] != conn.driver):
            return step
    if conn.secret_unreadable:
        raise UndecryptableSecret(f"Verbindung '{conn.name}': Secret nicht entschlüsselbar — neu eintragen")
    out = dict(step)
    for step_key, attr in _STEP_FIELDS:
        value = getattr(conn, attr)
        if value and not out.get(step_key):
            out[step_key] = value
    if conn.secret and not out.get("password"):
        out["password"] = conn.secret
    return out
