"""Der Fetcher (ADR 0060 §1): gehärteter HTTP-Get + Orchestrierung.

Fluss: Auto-Freigabe prüfen → SSRF-Schutz → Get (Redirects regeprüft) →
Content-Extraktion → Stufe-A-Bereinigung → Stufe-B-Scan → Status →
Quelle/Quarantäne schreiben + Herkunftsprotokoll.

Bewusst dependency-arm: HTTP via :mod:`urllib`, HTML via :mod:`html.parser`,
SSRF via :mod:`ipaddress`. PDF wird soft-gefehlt (braucht ``pypdf``).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from . import clean
from .scan import ScanFinding
from .scan import verdict as _verdict

FETCHER_VERSION = "1"
DEFAULT_TIMEOUT_S = 30
DEFAULT_UA = "fabrik-fetcher/" + FETCHER_VERSION + " (research; untrusted-content)"
MAX_BODY_BYTES = 2_000_000  # Cap, damit eine riesige Seite den Run nicht sprengt

_SNIPPET_LEN = 400


@dataclass
class FetchResult:
    url: str
    status: str  # clean | suspicious | quarantined | not_fetched | error
    findings: list[ScanFinding] = field(default_factory=list)
    source_path: Path | None = None
    fetched_at: str | None = None
    content_type: str | None = None
    reason: str | None = None  # für not_fetched / error
    snippet: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "clean"


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    for d in domains:
        d = d.lower().rstrip(".")
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → defensively block
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_public_url(url: str) -> tuple[str, str]:
    """URL validieren (http/https, Host auflösen, keine privaten/loopback IPs).

    Liefert ``(host, url)``; wirft :class:`ValueError` bei Verstoß.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Scheme {parsed.scheme!r} nicht erlaubt (nur http/https)")
    host = parsed.hostname
    if not host:
        raise ValueError("URL hat keinen Host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"Host {host!r} nicht auflösbar: {e}") from e
    for info in infos:
        ip = str(info[4][0])
        if _is_blocked_ip(ip):
            raise ValueError(f"SSRF-Schutz: {host!r} auflöst auf blockierte Adresse {ip} (privat/loopback/link-local)")
    return host, url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Folgt Redirects nur, wenn das Ziel erneut den SSRF-Schutz besteht."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        try:
            assert_public_url(newurl)
        except ValueError as e:
            raise urllib.error.HTTPError(newurl, code, f"Redirect auf blockiertes Ziel: {e}", headers, fp) from e
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    """Ein gehärteter Fetcher mit Auto-Freigaben, SSRF-Schutz und Herkunftsprotokoll."""

    def __init__(
        self,
        *,
        approved_domains: Iterable[str] = (),
        approved_urls: Iterable[str] = (),
        out_dir: Path | str | None = None,
        ledger_path: Path | str | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.approved_domains = {d.lower() for d in approved_domains}
        self.approved_urls = set(approved_urls)
        self.out_dir = Path(out_dir) if out_dir else None
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.timeout_s = timeout_s

    # --- Auto-Freigabe (ADR 0060 §3) ---
    def is_approved(self, url: str) -> bool:
        """Herkunft (explizite approved_urls) + approved_domains. Sonst: kein Fetch."""
        if url in self.approved_urls:
            return True
        host = urllib.parse.urlsplit(url).hostname or ""
        return _host_matches(host, self.approved_domains)

    # --- HTTP ---
    def _http_get(self, url: str) -> tuple[bytes, str]:
        """Geprüftes HTTP-Get; liefert ``(body, content_type)``."""
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA, "Accept": "text/html,text/plain,application/json,*/*;q=0.8"})
        opener = urllib.request.build_opener(_SafeRedirectHandler)
        with opener.open(req, timeout=self.timeout_s) as resp:
            body = resp.read(MAX_BODY_BYTES + 1)
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if len(body) > MAX_BODY_BYTES:
            body = body[:MAX_BODY_BYTES]
        return body, ctype

    # --- Content-Extraktion ---
    def _extract_text(self, body: bytes, ctype: str) -> str:
        if ctype == "text/html" or (not ctype and body.lstrip()[:1] == b"<"):
            return clean.clean_html(body.decode("utf-8", "replace"))
        if ctype == "application/pdf":
            try:
                import pypdf  # type: ignore
            except Exception as e:
                raise ValueError(f"PDF-Ergebnis benötigt pypdf (nicht installiert): {e}") from e
            from io import BytesIO

            reader = pypdf.PdfReader(BytesIO(body))
            return clean.normalize_text("\n".join((page.extract_text() or "") for page in reader.pages))
        return clean.normalize_text(body.decode("utf-8", "replace"))

    # --- Protokoll + Ausgabe ---
    def _write(self, result: FetchResult, text: str) -> None:
        if self.out_dir is None:
            return
        base = self.out_dir / ("sources" if result.status == "clean" else "quarantine")
        base.mkdir(parents=True, exist_ok=True)
        host = urllib.parse.urlsplit(result.url).hostname or "host"
        digest = hashlib.sha1(result.url.encode()).hexdigest()[:10]
        path = base / f"{host}-{digest}.md"
        header = (
            f"<!-- fabrik fetcher v{FETCHER_VERSION} — UNTRUSTED external content -->\n"
            f"<!-- url: {result.url} -->\n"
            f"<!-- fetched_at: {result.fetched_at} -->\n"
            f"<!-- content_type: {result.content_type} -->\n"
            f"<!-- scan: {result.status} -->\n"
        )
        if result.findings:
            header += "<!-- findings:\n"
            for f in result.findings:
                header += f"<!--   {f.severity} {f.rule}: {f.snippet} -->\n"
            header += "-->\n"
        path.write_text(header + "\n" + text + "\n", encoding="utf-8")
        result.source_path = path

    def _ledger(self, result: FetchResult, search_query: str | None, requested_by: str) -> None:
        if self.ledger_path is None:
            return
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "url": result.url,
            "fetched_at": result.fetched_at,
            "search_query": search_query,
            "requested_by": requested_by,
            "verdict": result.status,
            "fetcher_version": FETCHER_VERSION,
        }
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # --- Orchestrierung ---
    def fetch(self, url: str, *, search_query: str | None = None, requested_by: str = "cli") -> FetchResult:
        url = url.strip()
        if not self.is_approved(url):
            return FetchResult(url=url, status="not_fetched", reason="nicht freigegeben (kein approved_domain / approved_url)")
        try:
            assert_public_url(url)
        except ValueError as e:
            return FetchResult(url=url, status="error", reason=str(e))
        try:
            body, ctype = self._http_get(url)
        except Exception as e:  # Netzwerk/HTTP-Fehler → geordneter Fehler, kein Traceback
            return FetchResult(url=url, status="error", reason=f"{type(e).__name__}: {e}")
        # Scan läuft auf dem ROHEN Content (CSS-Hidden-Text, <system>-Marker und
        # JS-Payloads sitzen im Rohtext, nicht im bereinigten Text). Bereinigung
        # erzeugt den sauberen Text, den der Agent sieht.
        raw_text = body.decode("utf-8", "replace")
        findings = _run_scan(raw_text)
        status = _verdict(findings)
        try:
            clean_text = self._extract_text(body, ctype)
        except ValueError as e:
            return FetchResult(url=url, status="error", content_type=ctype, reason=str(e))
        now = config.utcnow().isoformat()
        result = FetchResult(
            url=url,
            status=status,
            findings=findings,
            fetched_at=now,
            content_type=ctype or None,
            snippet=clean_text[:_SNIPPET_LEN],
        )
        self._write(result, clean_text)
        self._ledger(result, search_query, requested_by)
        return result


def _run_scan(text: str) -> list[ScanFinding]:
    from .scan import scan

    return scan(text)
