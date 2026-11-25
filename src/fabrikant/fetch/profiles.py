"""Access profiles per loop/run + one-time grants (ADR 0060, stages 3–4).

A **profile** bundles what a run may additionally do (``access:`` block of the
project spec). Three sides:

- ``fetch``  → configures only the fetcher (no egress in the agent container).
- ``egress`` → extends the allowlist only for runs of this loop (proxy per run).
- ``env``/``tools`` → credentials from named connections / ``--allowedTools``.

The ``egress``/``env``/``tools`` sides are **parsed and stored** here but
not applied — the deployment (sandbox/proxy) layer does that (ADR 0060 §4). The
``fetch`` side is handed to the fetcher here (``fetcher_config_for``).

**One-time grants** (stage 4): a run that needs a profile its loop does not have
requests it as an ADR 0059 ``access`` escalation. A grant applies to an **item with expiry**
("PR #123, 24 h" or "relay task #45, next 3 sessions"), not to a single run.
Every use is logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# --- Defaults (ADR 0060 §4: research-web-Beispiel) ----------------------------
DEFAULT_FETCHES_PER_SESSION = 40
DEFAULT_QUERIES_PER_SESSION = 15


def _s(x: object) -> str:
    return x.strip() if isinstance(x, str) and x.strip() else ""


def _to_tuple_str(x: object) -> tuple[str, ...]:
    if x is None:
        return ()
    if isinstance(x, str):
        return (x,) if x.strip() else ()
    if isinstance(x, (list, tuple)):
        return tuple(v for v in (_s(i) for i in x) if v)
    return ()


def _to_int(x: object, default: int) -> int:
    if isinstance(x, bool):
        return default
    if isinstance(x, int) and x > 0:
        return x
    return default


# --- Schreib-Credential-Heuristik (ADR 0060 §4, Validierung) ------------------
_SECRET_MARKERS = ("secret", "key", "token", "password", "passwd", "credential", "write")
# Matched on whole `_`-separated parts, never as substrings: `PUBLIC_KEY` (an asymmetric
# public key) is harmless, but `GITHUB_PUBLIC_REPO_TOKEN` is a write token for public repos.
_PUBLIC_KEY_SEQUENCES = (("PUBLIC", "KEY"), ("PUBLIC", "KEYS"))
_PUBLIC_KEY_WORDS = ("PUBLICKEY", "PUBKEY")
_READONLY_SEQUENCES = (("READ", "ONLY"),)
_READONLY_WORDS = ("RO", "READONLY")


def _has_sequence(parts: list[str], seq: tuple[str, ...]) -> bool:
    n = len(seq)
    return any(tuple(parts[i : i + n]) == seq for i in range(len(parts) - n + 1))


def is_readonly_credential(env_key: str) -> bool:
    """Read-only if a whole part is ``RO``/``READONLY`` or ``READ``+``ONLY`` are adjacent
    (e.g. ``langfuse-ro`` → ``LANGFUSE_RO``, ``GITHUB_READ_ONLY_TOKEN``)."""
    parts = env_key.upper().split("_")
    return any(w in parts for w in _READONLY_WORDS) or any(_has_sequence(parts, s) for s in _READONLY_SEQUENCES)


def _is_public_key(env_key: str) -> bool:
    parts = env_key.upper().split("_")
    return any(w in parts for w in _PUBLIC_KEY_WORDS) or any(_has_sequence(parts, s) for s in _PUBLIC_KEY_SEQUENCES)


def is_write_credential(env_key: str) -> bool:
    """Write credential: looks like a secret and is neither read-only nor a public *key*."""
    low = env_key.lower()
    if not any(m in low for m in _SECRET_MARKERS):
        return False
    return not (is_readonly_credential(env_key) or _is_public_key(env_key))


# --- Modelle -------------------------------------------------------------------
@dataclass(frozen=True)
class FetchProfile:
    """The ``fetch`` side of a profile: what the fetcher may do."""

    approved_domains: tuple[str, ...] = ()
    fetches_per_session: int = DEFAULT_FETCHES_PER_SESSION
    queries_per_session: int = DEFAULT_QUERIES_PER_SESSION
    has_classifier: bool = False  # stage C (optional), flag only

    @classmethod
    def from_dict(cls, data: object) -> FetchProfile | None:
        if not isinstance(data, dict):
            return None
        doms = _to_tuple_str(data.get("approved_domains"))
        limits_raw = data.get("limits")
        limits = limits_raw if isinstance(limits_raw, dict) else {}
        classifier = data.get("classifier")
        return cls(
            approved_domains=doms,
            fetches_per_session=_to_int(limits.get("fetches_per_session"), DEFAULT_FETCHES_PER_SESSION),
            queries_per_session=_to_int(limits.get("queries_per_session"), DEFAULT_QUERIES_PER_SESSION),
            has_classifier=classifier is not None and bool(classifier),
        )

    def to_dict(self) -> dict:
        return {
            "approved_domains": list(self.approved_domains),
            "limits": {
                "fetches_per_session": self.fetches_per_session,
                "queries_per_session": self.queries_per_session,
            },
            "classifier": self.has_classifier,
        }


@dataclass
class AccessProfile:
    """A complete access profile (name + all three sides)."""

    name: str
    fetch: FetchProfile | None = None
    egress: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)  # env-key → connection-name
    tools: tuple[str, ...] = ()

    def carries_write_credentials(self) -> bool:
        """True if the ``env`` side carries a write credential (validation, §4)."""
        return any(is_write_credential(k) for k in self.env)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "fetch": self.fetch.to_dict() if self.fetch else None,
            "egress": list(self.egress),
            "env": dict(self.env),
            "tools": list(self.tools),
        }

    @classmethod
    def from_dict(cls, data: object) -> AccessProfile | None:
        if not isinstance(data, dict):
            return None
        name = _s(data.get("name"))
        if not name:
            return None
        fetch_raw = data.get("fetch")
        fetch = FetchProfile.from_dict(fetch_raw) if fetch_raw is not None else None
        env_raw_x = data.get("env")
        env_raw = env_raw_x if isinstance(env_raw_x, dict) else {}
        env = {k: _s(v) for k, v in env_raw.items() if _s(k) and _s(v)}
        return cls(
            name=name,
            fetch=fetch,
            egress=_to_tuple_str(data.get("egress")),
            env=env,
            tools=_to_tuple_str(data.get("tools")),
        )


# --- Spec-Parsing & Resolution -------------------------------------------------
def parse_access_spec(spec: object) -> dict[str, AccessProfile]:
    """The ``access:``-block of the spec → {name: AccessProfile}. Defensive: ``{}`` on bad input."""
    if not isinstance(spec, dict):
        return {}
    raw = spec.get("access")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, AccessProfile] = {}
    for name, body in raw.items():
        if not _s(name):
            continue
        prof = AccessProfile.from_dict({"name": name, **(body if isinstance(body, dict) else {})})
        if prof is not None:
            out[name] = prof
    return out


def profile_names_for_loop(spec: object, loop_name: str) -> list[str]:
    """Which profiles a loop has (``loops.<loop>.access``; relay is a special case)."""
    if not isinstance(spec, dict) or not _s(loop_name):
        return []
    raw: object = None
    loops = spec.get("loops")
    if isinstance(loops, dict) and isinstance(loops.get(loop_name), dict):
        raw = loops[loop_name].get("access")
    elif loop_name.lower() == "relay" and isinstance(spec.get("relay"), dict):
        raw = spec["relay"].get("access")
    return list(_to_tuple_str(raw))


def resolve_profiles(spec: object, loop_name: str) -> list[AccessProfile]:
    """Loop profile names → profile definitions (unknown names are omitted)."""
    profiles = parse_access_spec(spec)
    return [profiles[n] for n in profile_names_for_loop(spec, loop_name) if n in profiles]


def validate_access_spec(spec: object) -> list[str]:
    """Spec validation (ADR 0060 §4).

    Rule: a profile with ``egress`` and a profile with ``fetch`` must not be combined in
    the same loop if the ``egress`` profile carries write credentials. Whoever reads
    foreign content gets no write access to systems outside the branch.
    """
    problems: list[str] = []
    profiles = parse_access_spec(spec)
    if not isinstance(spec, dict):
        return ["spec is not a dict"]
    # Per loop: fetch profiles + egress profile with write creds → conflict.
    loop_map: dict[str, list[str]] = {}
    loops = spec.get("loops")
    if isinstance(loops, dict):
        for ln, lb in loops.items():
            if isinstance(lb, dict):
                loop_map[_s(ln)] = list(_to_tuple_str(lb.get("access")))
    if isinstance(spec.get("relay"), dict):
        loop_map["relay"] = list(_to_tuple_str(spec["relay"].get("access")))
    for loop_name, names in sorted(loop_map.items()):
        fetch_profs = [n for n in names if profiles.get(n) and profiles[n].fetch is not None]
        write_egress = [
            n for n in names if profiles.get(n) and profiles[n].egress and profiles[n].carries_write_credentials()
        ]
        if fetch_profs and write_egress:
            problems.append(
                f"Loop {loop_name!r}: fetch profile {fetch_profs} + egress profile with write credentials {write_egress} "
                f"must not be combined (ADR 0060 §4)"
            )
    return problems


# --- Fetcher-Anbindung ---------------------------------------------------------
def fetcher_config_for(profiles: list[AccessProfile]) -> dict:
    """Merged fetcher config from active profiles.

    Domains = union; limits = the strictest minimum (the most restrictive value wins).
    Without a ``fetch`` profile → empty domains (nothing approved).
    """
    fetch_profs = [p.fetch for p in profiles if p.fetch is not None]
    if not fetch_profs:
        return {
            "approved_domains": [],
            "fetches_per_session": DEFAULT_FETCHES_PER_SESSION,
            "queries_per_session": DEFAULT_QUERIES_PER_SESSION,
        }
    domains: set[str] = set()
    for fp in fetch_profs:
        domains.update(fp.approved_domains)
    return {
        "approved_domains": sorted(domains),
        "fetches_per_session": min(fp.fetches_per_session for fp in fetch_profs),
        "queries_per_session": min(fp.queries_per_session for fp in fetch_profs),
    }


# --- Stage 4: one-time grants -------------------------------------------------
@dataclass
class Grant:
    """A one-time grant for a profile, valid for one item, with expiry."""

    profile_name: str
    item: str  # z. B. "PR #123" / "Relay-Task #45"
    granted_at: datetime
    expires_at: datetime | None = None  # Zeit-Ablauf
    max_sessions: int | None = None  # Sessions-Ablauf
    used_sessions: int = 0

    def is_active(self, now: datetime) -> bool:
        if self.expires_at is not None and now > self.expires_at:
            return False
        if self.max_sessions is not None:
            return self.used_sessions < self.max_sessions
        return True

    def record_use(self) -> None:
        """Record one use (counts against ``max_sessions``)."""
        if self.max_sessions is not None:
            self.used_sessions += 1

    def to_dict(self) -> dict:
        return {
            "profile_name": self.profile_name,
            "item": self.item,
            "granted_at": self.granted_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "max_sessions": self.max_sessions,
            "used_sessions": self.used_sessions,
        }

    @classmethod
    def from_dict(cls, data: object) -> Grant | None:
        if not isinstance(data, dict):
            return None
        pn, item = _s(data.get("profile_name")), _s(data.get("item"))
        if not pn or not item:
            return None
        granted_at = _parse_dt(data.get("granted_at"))
        if granted_at is None:
            return None
        return cls(
            profile_name=pn,
            item=item,
            granted_at=granted_at,
            expires_at=_parse_dt(data.get("expires_at")),
            max_sessions=_to_int(data.get("max_sessions"), 0) or None,
            used_sessions=_to_int(data.get("used_sessions"), 0),
        )


def _parse_dt(x: object) -> datetime | None:
    # Naive datetimes are treated as UTC so aware/naive comparisons never crash.
    if isinstance(x, datetime):
        return x if x.tzinfo is not None else x.replace(tzinfo=UTC)
    if isinstance(x, str):
        try:
            dt = datetime.fromisoformat(x)
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return None


class GrantStore:
    """Sammlung einmaliger Grants (persistierbar, defensive)."""

    def __init__(self, grants: list[Grant] | None = None):
        self.grants: list[Grant] = list(grants or [])

    def grant(
        self,
        profile_name: str,
        item: str,
        *,
        now: datetime,
        ttl_hours: float | None = None,
        max_sessions: int | None = None,
    ) -> Grant:
        if ttl_hours is None and max_sessions is None:
            raise ValueError(
                "Grant needs a bound (ttl_hours or max_sessions) — forever grants are not allowed (ADR 0060 §5)"
            )
        g = Grant(
            profile_name=profile_name,
            item=item,
            granted_at=now,
            expires_at=now + timedelta(hours=ttl_hours) if ttl_hours is not None else None,
            max_sessions=max_sessions,
        )
        self.grants.append(g)
        return g

    def active_grants(self, now: datetime) -> list[Grant]:
        return [g for g in self.grants if g.is_active(now)]

    def active_profile_names(self, now: datetime) -> set[str]:
        return {g.profile_name for g in self.active_grants(now)}

    def effective_profiles(self, spec: object, loop_name: str, now: datetime) -> list[AccessProfile]:
        """Static loop profiles + active grant profiles (grants for defined profiles only)."""
        profiles = parse_access_spec(spec)
        names = set(profile_names_for_loop(spec, loop_name)) | self.active_profile_names(now)
        return [profiles[n] for n in sorted(names) if n in profiles]

    def record_usage(self, profile_name: str, item: str, now: datetime) -> bool:
        """Record a grant use. True if a matching active grant was counted."""
        for g in self.active_grants(now):
            if g.profile_name == profile_name and g.item == item:
                g.record_use()
                return True
        return False

    def to_dict(self) -> dict:
        return {"grants": [g.to_dict() for g in self.grants]}

    @classmethod
    def from_dict(cls, data: object) -> GrantStore | None:
        if not isinstance(data, dict):
            return None
        raw = data.get("grants")
        if not isinstance(raw, list):
            return None
        return cls([g for g in (Grant.from_dict(x) for x in raw) if g is not None])


# re-export for tests/CLI
__all__ = [
    "DEFAULT_FETCHES_PER_SESSION",
    "DEFAULT_QUERIES_PER_SESSION",
    "AccessProfile",
    "FetchProfile",
    "Grant",
    "GrantStore",
    "fetcher_config_for",
    "is_readonly_credential",
    "is_write_credential",
    "parse_access_spec",
    "profile_names_for_loop",
    "resolve_profiles",
    "validate_access_spec",
]
