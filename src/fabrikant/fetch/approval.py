"""Stufe 2 (ADR 0060 §3, ADR 0059 §5a): gebündelte menschliche Freigabe.

Eine Session, die URLs abrufen will, die weder im Herkunftsprotokoll noch in der
Freigabeliste stehen, bekommt für ALL diese URLs **einen** gebündelten `access`-
Request — nicht einen pro URL (Gegen Ermüdung). Besonderheiten gegenüber den anderen
Eskalationsarten (ADR 0059 §5a):

- **Kein Label, kein Stop.** Das Item läuft weiter; offene URLs bleiben unabgerufen.
- **Ablauf.** Nach `access_timeout` (Default 24 h) gilt der Request als abgelehnt.
- **Grant/Reject** pro URL oder „all". Grant-Information erreicht nie den Agenten als
  untrusted Input (kein PR-/Issue-Kommentar).
- **Suggest domain** ist nur ein Hint (Spec-Änderung durch einen Admin), kein Lerneffekt.

Deterministisch, ohne LLM. Der Request hängt am bestehenden `ApprovalRequest`-Modell
(ADR 0059) ab, damit er durch die gleiche UI-Operation läuft.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fabrikant.approval import ApprovalRequest

#: Default-Ablauf für offene `access`-Requests (ADR 0059 §5a).
ACCESS_TIMEOUT_HOURS = 24.0

#: Mindestanzahl wiederholter Grants pro Domain, bevor „Suggest domain" einen
#: Hinweis liefert (ADR 0060 §3, „Gegen Ermüdung").
SUGGEST_DOMAIN_THRESHOLD = 2


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class PendingURL:
    """Eine URL, die Freigabe braucht (ADR 0060 §3)."""

    url: str
    domain: str
    found_in: str  # wo gefunden: source-id, "search" oder "agent"
    scan: str  # Scan-Ergebnis dieser Quelle (clean/suspicious/quarantined/"")
    why: str  # Begründung des Agenten

    @classmethod
    def from_dict(cls, data: object) -> PendingURL | None:
        if not isinstance(data, dict):
            return None
        url = data.get("url")
        if not isinstance(url, str) or not url:
            return None
        return cls(
            url=url,
            domain=str(data.get("domain") or ""),
            found_in=str(data.get("found_in") or ""),
            scan=str(data.get("scan") or ""),
            why=str(data.get("why") or ""),
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class AccessRequest:
    """Ein gebündelter `access`-Freigabe-Request für ein Item (ADR 0059 §5a)."""

    item: str
    urls: list[PendingURL] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    timeout_hours: float = ACCESS_TIMEOUT_HOURS
    decided: dict[str, str] = field(default_factory=dict)  # url -> "granted" | "rejected"
    status: str = "open"  # open | resolved | expired

    # --- Anblick / Selektion ---------------------------------------------------
    def pending(self) -> list[PendingURL]:
        """Noch unentschiedene URLs (Reihenfolge wie beantragt)."""
        return [u for u in self.urls if self.decided.get(u.url) not in ("granted", "rejected")]

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or _utcnow()
        return self.created_at + timedelta(hours=self.timeout_hours) < now

    def check_expiry(self, now: datetime | None = None) -> None:
        """Ablauf anwenden: offener Request älter als `timeout_hours` → `expired`."""
        if self.status == "open" and self.is_expired(now):
            self.status = "expired"

    # --- Entscheidungen --------------------------------------------------------
    def _apply(self, urls: str | list[str] | tuple[str, ...], decision: str) -> list[str]:
        if urls == "all":
            targets = [u.url for u in self.pending()]
        elif isinstance(urls, str):
            targets = [urls]
        else:
            targets = list(urls)
        applied = []
        for url in targets:
            if self.decided.get(url) in ("granted", "rejected"):
                continue  # bereits entschieden — nicht doppelt
            if any(u.url == url for u in self.urls):
                self.decided[url] = decision
                applied.append(url)
        self._maybe_resolve()
        return applied

    def grant(self, urls: str | list[str]) -> list[str]:
        """Freigeben (pro URL oder „all"). Returniert die freigegebenen URLs."""
        return self._apply(urls, "granted")

    def reject(self, urls: str | list[str]) -> list[str]:
        """Ablehnen (pro URL oder „all"). Returniert die abgelehnten URLs."""
        return self._apply(urls, "rejected")

    def _maybe_resolve(self) -> None:
        if self.status == "open" and not self.pending():
            self.status = "resolved"

    def granted_urls(self) -> list[str]:
        return [u for u, d in self.decided.items() if d == "granted"]

    def rejected_urls(self) -> list[str]:
        return [u for u, d in self.decided.items() if d == "rejected"]

    # --- Domain-Statistik / „Suggest domain" (Hint, kein Lerneffekt) -----------
    def domain_stats(self) -> dict[str, int]:
        """Entscheidungen (Grant + Reject) pro Domain."""
        stats: Counter = Counter()
        for u in self.urls:
            if self.decided.get(u.url) in ("granted", "rejected"):
                stats[u.domain] += 1
        return dict(stats)

    def suggest_domain(self, threshold: int = SUGGEST_DOMAIN_THRESHOLD) -> str | None:
        """Häufigste freigegebene Domain (>= `threshold` Grants) als Hint.

        Nur Hinweis für eine Spec-Änderung (Admin nimmt die Domain in die
        Freigabeliste des Profils auf) — keine automatische Aufnahme.
        """
        granted = Counter(u.domain for u in self.urls if self.decided.get(u.url) == "granted")
        if not granted:
            return None
        top, count = granted.most_common(1)[0]
        return top if count >= threshold else None

    # --- Anhängen (spätere Session, gleicher Item) ----------------------------
    def append(self, new_urls: list[PendingURL], now: datetime | None = None) -> list[PendingURL]:
        """Neue URLs einer späteren Session anhängen (ADR 0059 §5a: „appended").

        Bereits bekannte URLs werden nicht dupliziert. Returniert die tatsächlich
        neu hinzugekommenen.
        """
        known = {u.url for u in self.urls}
        added = []
        for u in new_urls:
            if u.url not in known:
                self.urls.append(u)
                known.add(u.url)
                added.append(u)
        if added and self.status == "expired":
            # Ein abgelaufener Request lebt für neue URLs nicht auf — bleibt abgelehnt.
            pass
        return added

    # --- Koppelung an das ADR-0059-Modell -------------------------------------
    def to_approval_request(self) -> ApprovalRequest:
        """Als `ApprovalRequest` (reason „escalation", kind „access") für die UI."""
        pending = self.pending()
        domains: Counter = Counter(u.domain for u in pending)
        what = ", ".join(f"{d}: {n}" for d, n in domains.most_common())
        return ApprovalRequest(
            reason="escalation",
            kind="access",
            step="fetch",
            why=f"{len(pending)} URL(s) brauchen Freigabe (Item {self.item})",
            what=what,
            permissions=[],
        )

    # --- Persistenz ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "urls": [u.to_dict() for u in self.urls],
            "created_at": self.created_at.isoformat(),
            "timeout_hours": self.timeout_hours,
            "decided": dict(self.decided),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: object) -> AccessRequest | None:
        if not isinstance(data, dict):
            return None
        item = data.get("item")
        if not isinstance(item, str) or not item:
            return None
        urls = [p for p in (PendingURL.from_dict(u) for u in (data.get("urls") or [])) if p]
        decided_raw = data.get("decided")
        decided = (
            {k: v for k, v in decided_raw.items() if isinstance(k, str) and v in ("granted", "rejected")}
            if isinstance(decided_raw, dict)
            else {}
        )
        timeout = data.get("timeout_hours")
        status = data.get("status")
        return cls(
            item=item,
            urls=urls,
            created_at=_parse_dt(data.get("created_at")) or _utcnow(),
            timeout_hours=float(timeout)
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
            else ACCESS_TIMEOUT_HOURS,
            decided=decided,
            status=status if status in ("open", "resolved", "expired") else "open",
        )


def bundle_access(
    item: str, pending: list[PendingURL] | list[dict], now: datetime | None = None
) -> AccessRequest | None:
    """Aus den offenen URLs einer Session EINEN gebündelten Request bauen.

    Duplizierte URLs fallen raus. Keine offenen URLs → ``None`` (nichts zu freigegeben).
    """
    urls: list[PendingURL] = []
    seen: set[str] = set()
    for p in pending:
        u = p if isinstance(p, PendingURL) else PendingURL.from_dict(p)
        if u is None or u.url in seen:
            continue
        seen.add(u.url)
        urls.append(u)
    if not urls:
        return None
    return AccessRequest(item=item, urls=urls, created_at=now or _utcnow())
