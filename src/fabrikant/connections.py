"""Named connections (ADR 0052): LLM providers and agent backends, managed in the UI.

A :class:`Connection` bundles driver, endpoint, model and access under one name. It lives in
the ``connections`` table of the SQL store (secret Fernet-encrypted, key from
``FABRIKANT_SECRET_KEY`` or ``state/secret.key``). Env configuration (``FABRIKANT_LLM_*``,
``FABRIKANT_AGENT_*``) stays valid and appears as a read-only connection; on name clashes
the UI connection wins. Resolution happens before dispatch: :func:`apply_to_step` writes the
connection values into an agent step so the backends stay unchanged.
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
from .i18n import t as _t
from .runschema import connections as connections_table

log = logging.getLogger("fabrikant.connections")

SECRET_KEY_ENV = "FABRIKANT_SECRET_KEY"
SECRET_KEY_FILE = "secret.key"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
ENV_AGENT_NAME = "env"
DRIVER_HTTP = "http"  # LLM: OpenAI-compatible endpoint (counterpart to the CLI drivers)
AGENT_STEP_TYPES = frozenset({"agent", "review", "pr_review"})


class InvalidConnection(ValueError):
    """Invalid or unknown connection."""


class UndecryptableSecret(InvalidConnection):
    """Stored secret cannot be decrypted with the current key."""


class ConnectionKind(StrEnum):
    LLM = "llm"
    AGENT = "agent"


class Source(StrEnum):
    DB = "db"
    ENV = "env"


@dataclass(frozen=True)
class Connection:
    """A named connection. ``driver``: agent → backend name; LLM → ``http`` or CLI name."""

    name: str
    kind: ConnectionKind
    driver: str = ""
    endpoint: str = ""
    model: str = ""
    username: str = ""
    secret: str = field(default="", repr=False)
    is_default: bool = False
    source: Source = Source.DB
    secret_unreadable: bool = False  # stored but not decryptable (key changed/lost)

    def validate(self) -> None:
        """Check required fields per kind/driver; raises :class:`InvalidConnection`."""
        from . import llm_cli
        from .backends.registry import known_backends

        if not NAME_RE.match(self.name):
            raise InvalidConnection(_t("connections.name_1_48_characters_from"))
        if self.kind is ConnectionKind.AGENT:
            if self.driver not in known_backends():
                raise InvalidConnection(
                    _t("connections.backend_unknown_available", v1=self.driver, v2=", ".join(known_backends()))
                )
            if self.driver == "opencode" and not (self.endpoint and self.model):
                raise InvalidConnection(_t("connections.opencode_needs_endpoint_and_model"))
        elif self.driver in llm_cli.CLIS:
            if not self.model:
                raise InvalidConnection(_t("connections.model_missing"))
        elif self.driver == DRIVER_HTTP:
            if not (self.endpoint and self.model):
                raise InvalidConnection(_t("connections.http_providers_need_endpoint_and"))
        else:
            raise InvalidConnection(
                _t("connections.llm_driver_unknown_http", v1=self.driver, v2=", ".join(llm_cli.CLIS))
            )

    def public(self) -> dict[str, Any]:
        """API form without secret (``secret_set`` only shows WHETHER one is set)."""
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
        """Provider dict in the format of :mod:`fabrikant.llm` (``cli`` or ``url``)."""
        if self.driver == DRIVER_HTTP:
            return {"name": self.name, "url": self.endpoint, "model": self.model, "key": self.secret}
        return {"name": self.name, "url": "", "cli": self.driver, "model": self.model, "key": ""}

    @classmethod
    def from_payload(cls, name: str, kind: str, data: Mapping[str, Any]) -> Connection:
        """API payload → connection (without secret semantics; see :func:`save_connection`)."""
        try:
            parsed_kind = ConnectionKind(kind)
        except ValueError:
            raise InvalidConnection(_t("connections.kind_must_be_llm_or", kind=kind)) from None

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


# ---------------------------------------------------------------- encryption
_fernet_guard = threading.Lock()


def _key_file() -> Path:
    return config.STATE_DIR / SECRET_KEY_FILE


def _create_key_file(path: Path) -> None:
    """Create the key file atomically: first write completely to a temp file (0600), then publish via
    ``os.link``. This way no parallel process sees an empty file, and a
    simultaneous winner (``FileExistsError``) is not an error."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
        with contextlib.suppress(FileExistsError):  # another process was faster — its key applies
            os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _load_key() -> bytes:
    """Fernet key: ``FABRIKANT_SECRET_KEY``, otherwise key file (created with 0600 on first need)."""
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
    """Fernet token → plaintext. Not decryptable (key changed) → :class:`UndecryptableSecret`."""
    if not token:
        return ""
    try:
        return Fernet(_load_key()).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError) as e:
        raise UndecryptableSecret(
            _t("connections.secret_cannot_be_decrypted_wrong", v1=SECRET_KEY_ENV, v2=SECRET_KEY_FILE)
        ) from e


# ---------------------------------------------------------------- Persistenz
def _store():
    from .runstore_sql import store_for_url  # local: load SQLAlchemy only when used

    return store_for_url(config.db_url())


_secret_cache: list[str] | None = None
_secret_cache_at = 0.0  # monotonic; time of the last successful load
_secret_cache_failed_at: float | None = None  # monotonic; set when loading last failed
SECRET_CACHE_TTL_S = 30.0  # other fabrikant processes on the same DB see changes at the latest then
SECRET_RETRY_S = 30.0  # after an error, do not query the DB again for this long (masking runs per log line)


def _invalidate() -> None:
    global _secret_cache, _secret_cache_failed_at
    _secret_cache = None
    _secret_cache_failed_at = None


def _from_row(row: RowMapping, *, with_secret: bool = True) -> Connection:
    """DB row → connection. An undecryptable secret does not abort listings but is
    marked as ``secret_unreadable`` (``apply_to_step`` then rejects it with a clear message).
    ``with_secret=False`` does not decrypt at all (secret empty) — for display only."""
    secret, unreadable = "", False
    try:
        secret = decrypt(row["secret"]) if with_secret else ""
    except UndecryptableSecret:
        unreadable = True
        log.warning(_t("connections.connection_s_secret_cannot_be"), row["name"], SECRET_KEY_ENV, SECRET_KEY_FILE)
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
    """UI-managed connections (secret decrypted unless ``with_secret=False``), sorted by name."""
    q = select(connections_table).order_by(connections_table.c.name)
    if kind is not None:
        q = q.where(connections_table.c.kind == kind.value)
    with _store().read_tx() as conn:
        return [_from_row(r, with_secret=with_secret) for r in conn.execute(q).mappings()]


def env_connections() -> list[Connection]:
    """Read-only connections from the env configuration (agent ``env`` + LLM providers)."""
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
    backend = os.environ.get("FABRIKANT_AGENT_BACKEND", "").strip()
    if backend:
        out.append(
            Connection(
                name=ENV_AGENT_NAME,
                kind=ConnectionKind.AGENT,
                driver=backend,
                endpoint=os.environ.get("FABRIKANT_AGENT_ENDPOINT", "").strip(),
                model=os.environ.get("FABRIKANT_AGENT_MODEL", "").strip(),
                username=os.environ.get("FABRIKANT_AGENT_USERNAME", "").strip(),
                secret=os.environ.get("FABRIKANT_AGENT_PASSWORD", "").strip(),
                source=Source.ENV,
            )
        )
    return out


def list_connections(kind: ConnectionKind | None = None, *, with_secret: bool = True) -> list[Connection]:
    """All connections: env first, UI connections replace env entries of the same name."""
    merged: dict[tuple[ConnectionKind, str], Connection] = {}
    for c in env_connections() + db_connections(with_secret=with_secret):
        if kind is None or c.kind is kind:
            merged[(c.kind, c.name)] = c
    return list(merged.values())


def get_connection(name: str, kind: ConnectionKind, *, with_secret: bool = True) -> Connection | None:
    return next((c for c in list_connections(kind, with_secret=with_secret) if c.name == name), None)


def default_connection(kind: ConnectionKind, *, with_secret: bool = True) -> Connection | None:
    """UI connection of the kind marked as default (``None`` → env/legacy configuration applies)."""
    return next((c for c in db_connections(kind, with_secret=with_secret) if c.is_default), None)


def save_connection(conn: Connection, *, keep_secret: bool = False) -> Connection:
    """Create/replace a connection (after validation). ``keep_secret``: keep the existing secret.

    One default per kind: ``is_default`` clears the mark on the others."""
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
            raise InvalidConnection(_t("connections.already_exists_as_a_connection", v1=conn.name, v2=existing["kind"]))
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
    """Delete a UI connection; ``False`` = did not exist (env connections cannot be deleted)."""
    with _store().write_tx() as db:
        deleted = db.execute(delete(connections_table).where(connections_table.c.name == name)).rowcount
    _invalidate()
    return bool(deleted)


def secret_values() -> list[str]:
    """Decrypted secrets of all UI connections — for log/record masking (cached for :data:`SECRET_CACHE_TTL_S`).

    Errors (DB/key) never abort the caller; they are cached negatively (:data:`SECRET_RETRY_S`)
    and logged once per window, so not every log line queries the DB."""
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
        log.warning(_t("connections.connection_secrets_could_not_be"), exc_info=True)
        return []
    return _secret_cache


secrets.register_source(secret_values)


def unknown_references(spec: Mapping[str, Any]) -> list[str]:
    """Agent steps of the spec whose ``connection:`` does not exist (for ``fabrikant validate``).

    Host-dependent (DB + env of this host) — therefore a warning, not a spec error. Values with
    placeholders (``{var}``) are only resolved at runtime and not checked."""
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
    return [
        _t("connections.agent_connection_unknown", loop=loop, step=step, name=name)
        for loop, step, name in refs
        if name not in known
    ]


# ---------------------------------------------------------------- resolution in the step
_STEP_FIELDS = (("backend", "driver"), ("endpoint", "endpoint"), ("model", "model"), ("username", "username"))


def apply_to_step(step: dict, *, with_secret: bool = True) -> dict:
    """Complement an agent step with connection values (step fields win, env stays fallback).

    ``connection: <name>`` in the step selects explicitly (unknown or differing ``backend`` → error);
    without it the default connection applies, provided its backend matches the step.
    ``with_secret=False``: display fields only (no decrypting, no ``password``) — e.g. for the step intent."""
    if step.get("type") not in AGENT_STEP_TYPES:
        return step
    name = str(step.get("connection") or "").strip()
    if name:
        conn = get_connection(name, ConnectionKind.AGENT, with_secret=with_secret)
        if conn is None:
            raise InvalidConnection(_t("connections.agent_connection_unknown_2", name=name))
        if step.get("backend") and step["backend"] != conn.driver:
            raise InvalidConnection(
                _t("connections.backend_does_not_match_connection", v1=step["backend"], name=name, v3=conn.driver)
            )
    else:
        conn = default_connection(ConnectionKind.AGENT, with_secret=with_secret)
        if conn is None or (step.get("backend") and step["backend"] != conn.driver):
            return step
    if conn.secret_unreadable:
        raise UndecryptableSecret(_t("connections.connection_secret_cannot_be_decrypted", v1=conn.name))
    out = dict(step)
    for step_key, attr in _STEP_FIELDS:
        value = getattr(conn, attr)
        if value and not out.get(step_key):
            out[step_key] = value
    if conn.secret and not out.get("password"):
        out["password"] = conn.secret
    return out
