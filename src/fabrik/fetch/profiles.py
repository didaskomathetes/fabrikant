"""Zugriffsprofile pro Loop/Run + einmalige Grants (ADR 0060, Stufen 3–4).

Ein **Profil** fasst zusammen, was ein Run zusätzlich darf (``access:``-Block der
Projekt-Spec). Drei Seiten:

- ``fetch``  → konfiguriert nur den Fetcher (kein Egress im Agent-Container).
- ``egress`` → erweitert die Allowlist nur für Runs dieses Loops (Proxy pro Run).
- ``env``/``tools`` → Credentials aus benannten Connections / ``--allowedTools``.

Die ``egress``/``env``/``tools``-Seiten werden hier **geparst und gespeichert**, aber
nicht angewandt — das Deployment- (Sandbox-/Proxy-) Layer macht das (ADR 0060 §4). Die
``fetch``-Seite wird hier an den Fetcher übergeben (``fetcher_config_for``).

**Einmalige Grants** (Stufe 4): Ein Run, der ein Profil braucht, das sein Loop nicht hat,
beantragt es als ADR-0059-Eskalation ``access``. Ein Grant gilt für ein **Item mit Ablauf**
(„PR #123, 24 h" oder „Relay-Task #45, nächste 3 Sessions"), nicht für einen einzelnen Run.
Jede Nutzung wird protokolliert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

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


def is_readonly_credential(env_key: str) -> bool:
    """Read-only, wenn der Key das ``RO``-Marker trägt (z. B. ``langfuse-ro`` → ``LANGFUSE_RO``)."""
    parts = env_key.upper().split("_")
    return "RO" in parts


def is_write_credential(env_key: str) -> bool:
    """Schreib-Credential, wenn der Key wie ein Secret aussieht UND nicht read-only ist."""
    low = env_key.lower()
    if not any(m in low for m in _SECRET_MARKERS):
        return False
    return not is_readonly_credential(env_key)


# --- Modelle -------------------------------------------------------------------
@dataclass(frozen=True)
class FetchProfile:
    """Die ``fetch``-Seite eines Profils: was der Fetcher darf."""

    approved_domains: tuple[str, ...] = ()
    fetches_per_session: int = DEFAULT_FETCHES_PER_SESSION
    queries_per_session: int = DEFAULT_QUERIES_PER_SESSION
    has_classifier: bool = False  # Stufe C (optional), nur Flag

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
    """Ein vollständiges Zugriffsprofil (Name + alle drei Seiten)."""

    name: str
    fetch: FetchProfile | None = None
    egress: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)  # env-key → connection-name
    tools: tuple[str, ...] = ()

    def carries_write_credentials(self) -> bool:
        """True, wenn die ``env``-Seite ein Schreib-Credential trägt (Validierung, §4)."""
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
        return cls(name=name, fetch=fetch, egress=_to_tuple_str(data.get("egress")), env=env, tools=_to_tuple_str(data.get("tools")))


# --- Spec-Parsing & Resolution -------------------------------------------------
def parse_access_spec(spec: object) -> dict[str, AccessProfile]:
    """Der ``access:``-Block der Spec → {name: AccessProfile}. Defensive: ``{}`` bei Bad."""
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
    """Welche Profile ein Loop hat (``loops.<loop>.access``; Relay ist Sonderfall)."""
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
    """Profilnamen des Loops → Profile-Definitionen (unbekannte Namen werden weggelassen)."""
    profiles = parse_access_spec(spec)
    return [profiles[n] for n in profile_names_for_loop(spec, loop_name) if n in profiles]


def validate_access_spec(spec: object) -> list[str]:
    """Spec-Validierung (ADR 0060 §4).

    Regel: Ein Profil mit ``egress`` und ein Profil mit ``fetch`` dürfen nicht im selben
    Loop kombiniert werden, wenn das ``egress``-Profil Schreib-Credentials trägt. Wer
    fremde Inhalte liest, bekommt keine Schreibrechte auf Systeme außerhalb des Branches.
    """
    problems: list[str] = []
    profiles = parse_access_spec(spec)
    if not isinstance(spec, dict):
        return ["spec ist kein Dict"]
    # Pro Loop: fetch-Profile + egress-Profil mit Write-Creds → Konflikt.
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
        write_egress = [n for n in names if profiles.get(n) and profiles[n].egress and profiles[n].carries_write_credentials()]
        if fetch_profs and write_egress:
            problems.append(
                f"Loop {loop_name!r}: fetch-Profil {fetch_profs} + egress-Profil mit Write-Credentials {write_egress} "
                f"dürfen nicht kombiniert werden (ADR 0060 §4)"
            )
    return problems


# --- Fetcher-Anbindung ---------------------------------------------------------
def fetcher_config_for(profiles: list[AccessProfile]) -> dict:
    """Merged Fetcher-Konfig aus aktiven Profilen.

    Domains = Union; Limits = das strenge Minimum (der restriktivste Wert gewinnt).
    Ohne ``fetch``-Profil → leere Domains (nichts freigegeben).
    """
    fetch_profs = [p.fetch for p in profiles if p.fetch is not None]
    if not fetch_profs:
        return {"approved_domains": [], "fetches_per_session": DEFAULT_FETCHES_PER_SESSION, "queries_per_session": DEFAULT_QUERIES_PER_SESSION}
    domains: set[str] = set()
    for fp in fetch_profs:
        domains.update(fp.approved_domains)
    return {
        "approved_domains": sorted(domains),
        "fetches_per_session": min(fp.fetches_per_session for fp in fetch_profs),
        "queries_per_session": min(fp.queries_per_session for fp in fetch_profs),
    }


# --- Stufe 4: Einmalige Grants -------------------------------------------------
@dataclass
class Grant:
    """Ein einmaliger Grant für ein Profil, gültig für ein Item mit Ablauf."""

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
        """Eine Nutzung protokollieren (zählt gegen ``max_sessions``)."""
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
    if isinstance(x, datetime):
        return x
    if isinstance(x, str):
        try:
            return datetime.fromisoformat(x)
        except ValueError:
            return None
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
        """Statische Loop-Profile + aktive Grant-Profile (Grants für definierte Profile nur)."""
        profiles = parse_access_spec(spec)
        names = set(profile_names_for_loop(spec, loop_name)) | self.active_profile_names(now)
        return [profiles[n] for n in sorted(names) if n in profiles]

    def record_usage(self, profile_name: str, item: str, now: datetime) -> bool:
        """Nutzung eines Grants protokollieren. True, wenn ein passender aktiver Grant gezählt wurde."""
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


# Re-Export für Tests/CLI
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
