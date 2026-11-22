"""The fetcher (ADR 0060 §1): hardened HTTP GET + orchestration.

Flow: approval check → SSRF guard (pre-check + connected-peer check) → GET
(redirects re-checked and re-approved) → extraction → stage-A cleaning →
stage-B scan (raw + cleaned text) → verdict → write source/quarantine + ledger.

Dependency-light on purpose: HTTP via :mod:`urllib`, HTML via :mod:`html.parser`,
SSRF via :mod:`ipaddress`. PDF needs the optional ``pypdf``.
"""

from __future__ import annotations

import codecs
import hashlib
import http.client
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .. import config
from . import clean
from .approval import PendingURL
from .scan import ScanFinding, scan
from .scan import verdict as _verdict

FETCHER_VERSION = "1"
DEFAULT_TIMEOUT_S = 30
DEFAULT_UA = "fabrik-fetcher/" + FETCHER_VERSION + " (research; untrusted-content)"
MAX_BODY_BYTES = 2_000_000  # cap so a huge page cannot blow up the run
DEFAULT_DEADLINE_S = 60  # wall-clock limit for one whole fetch (slow-drip protection)
MAX_PDF_PAGES = 100
_READ_CHUNK = 64 * 1024
_SNIPPET_LEN = 400
_HEADER_FIELD_MAX = 300  # cap for attacker-controlled text written into file headers
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


class Status(StrEnum):
    """Outcome of a fetch (compares equal to its plain string value)."""

    CLEAN = "clean"
    SUSPICIOUS = "suspicious"
    QUARANTINED = "quarantined"
    NOT_FETCHED = "not_fetched"
    ERROR = "error"


@dataclass
class FetchResult:
    url: str
    status: str  # a Status value
    findings: list[ScanFinding] = field(default_factory=list)
    source_path: Path | None = None
    fetched_at: str | None = None
    content_type: str | None = None
    reason: str | None = None  # für not_fetched / error
    snippet: str = ""
    domain: str = ""  # Host der URL (für Freigabe/Domain-Statistik)
    found_in: str = ""  # Provenance: wo die URL herkam (source-id / "search" / "agent")

    @property
    def ok(self) -> bool:
        return self.status == Status.CLEAN


def _host_of(url: str) -> str:
    """Lower-cased hostname of a URL — for approval/statistics."""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    for d in domains:
        d = d.lower().rstrip(".")
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _is_blocked_ip(ip_str: str) -> bool:
    """True unless the address is globally routable (also unwraps embedded IPv4)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → defensively block
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is None and ip.teredo:
            embedded = ip.teredo[1]
        if embedded is not None:
            return _is_blocked_ip(str(embedded))
    return not ip.is_global or ip.is_multicast


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
        infos = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP
        )
    except socket.gaierror as e:
        raise ValueError(f"Host {host!r} nicht auflösbar: {e}") from e
    for info in infos:
        ip = str(info[4][0])
        if _is_blocked_ip(ip):
            raise ValueError(f"SSRF-Schutz: {host!r} auflöst auf blockierte Adresse {ip} (privat/loopback/link-local)")
    return host, url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only if the target passes the SSRF guard again and, when an
    ``approve`` callback is given, is itself approved (ADR 0060 §3)."""

    def __init__(self, approve=None) -> None:
        super().__init__()
        self._approve = approve

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            assert_public_url(newurl)
            if self._approve is not None and not self._approve(newurl):
                raise ValueError("redirect target not approved (approved_domains/approved_urls)")
        except ValueError as e:
            raise urllib.error.HTTPError(newurl, code, f"redirect to blocked target: {e}", headers, fp) from e
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_peer(sock: socket.socket) -> None:
    """Verify the *connected* peer address — closes the DNS-rebinding window between
    :func:`assert_public_url` and the actual connect. Nothing has been sent yet."""
    peer = str(sock.getpeername()[0])
    if _is_blocked_ip(peer):
        sock.close()
        raise OSError(f"SSRF guard: connected to blocked address {peer}")


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        super().connect()
        _check_peer(self.sock)


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        super().connect()
        _check_peer(self.sock)


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req)


def decode_body(body: bytes) -> str:
    """Decode by BOM (UTF-8/16/32), else UTF-8 with replacement."""
    for bom, codec in _BOMS:
        if body.startswith(bom):
            return body.decode(codec, "replace")
    return body.decode("utf-8", "replace")


def _header_safe(text: str) -> str:
    """Make attacker-controlled text safe inside an HTML-comment header line."""
    return text.replace("-->", "--&gt;").replace("\n", " ").replace("\r", " ")[:_HEADER_FIELD_MAX]


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
        deadline_s: int = DEFAULT_DEADLINE_S,
    ) -> None:
        self.approved_domains = {d.lower() for d in approved_domains}
        self.approved_urls = set(approved_urls)
        self.out_dir = Path(out_dir) if out_dir else None
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.timeout_s = timeout_s
        self.deadline_s = deadline_s

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
        req = urllib.request.Request(
            url, headers={"User-Agent": DEFAULT_UA, "Accept": "text/html,text/plain,application/json,*/*;q=0.8"}
        )
        opener = urllib.request.build_opener(
            _GuardedHTTPHandler, _GuardedHTTPSHandler, _SafeRedirectHandler(self.is_approved)
        )
        deadline = time.monotonic() + self.deadline_s
        chunks: list[bytes] = []
        size = 0
        with opener.open(req, timeout=self.timeout_s) as resp:
            while size <= MAX_BODY_BYTES:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"deadline of {self.deadline_s}s exceeded")
                chunk = resp.read(min(_READ_CHUNK, MAX_BODY_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return b"".join(chunks)[:MAX_BODY_BYTES], ctype

    # --- Content extraction ---
    def _extract_text(self, body: bytes, ctype: str) -> str:
        if ctype == "application/pdf":
            return self._extract_pdf(body)
        text = decode_body(body)
        if ctype == "text/html" or (not ctype and text.lstrip()[:1] == "<"):
            return clean.clean_html(text)
        return clean.normalize_text(text)

    @staticmethod
    def _extract_pdf(body: bytes) -> str:
        try:
            import pypdf  # type: ignore
        except Exception as e:
            raise ValueError(f"PDF result needs pypdf (not installed): {e}") from e
        from io import BytesIO

        try:
            reader = pypdf.PdfReader(BytesIO(body))
            pages = reader.pages[:MAX_PDF_PAGES]
            return clean.normalize_text("\n".join((page.extract_text() or "") for page in pages))
        except Exception as e:  # malformed PDFs raise many pypdf-specific errors
            raise ValueError(f"PDF unreadable: {type(e).__name__}: {e}") from e

    # --- Output + provenance ---
    def _write(self, result: FetchResult, text: str) -> None:
        if self.out_dir is None:
            return
        host = urllib.parse.urlsplit(result.url).hostname or "host"
        digest = hashlib.sha1(result.url.encode()).hexdigest()[:10]
        name = f"{host}-{digest}.md"
        target, other = ("sources", "quarantine") if result.status == Status.CLEAN else ("quarantine", "sources")
        base = self.out_dir / target
        base.mkdir(parents=True, exist_ok=True)
        (self.out_dir / other / name).unlink(missing_ok=True)  # a re-fetch changed the verdict
        path = base / name
        header = (
            f"<!-- fabrik fetcher v{FETCHER_VERSION} — UNTRUSTED external content -->\n"
            f"<!-- url: {_header_safe(result.url)} -->\n"
            f"<!-- fetched_at: {result.fetched_at} -->\n"
            f"<!-- content_type: {result.content_type} -->\n"
            f"<!-- scan: {result.status} -->\n"
        )
        if result.findings:
            header += "<!-- findings:\n"
            for f in result.findings:
                header += f"<!--   {f.severity} {f.rule}: {_header_safe(f.snippet)} -->\n"
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

    # --- Orchestration ---
    def _fail(self, result: FetchResult, search_query: str | None, requested_by: str) -> FetchResult:
        """Record a failed/blocked fetch in the ledger (attempts keep provenance)."""
        result.fetched_at = result.fetched_at or config.utcnow().isoformat()
        self._ledger(result, search_query, requested_by)
        return result

    def fetch(
        self, url: str, *, search_query: str | None = None, requested_by: str = "cli", found_in: str = ""
    ) -> FetchResult:
        url = url.strip()
        domain = _host_of(url)
        if not self.is_approved(url):
            return FetchResult(
                url=url,
                status=Status.NOT_FETCHED,
                domain=domain,
                found_in=found_in,
                reason="not approved (no approved_domain / approved_url)",
            )

        def fail(status: str, reason: str, ctype: str | None = None) -> FetchResult:
            return self._fail(
                FetchResult(
                    url=url, status=status, reason=reason, domain=domain, found_in=found_in, content_type=ctype
                ),
                search_query,
                requested_by,
            )

        try:
            assert_public_url(url)
        except ValueError as e:
            return fail(Status.ERROR, str(e))
        try:
            body, ctype = self._http_get(url)
        except Exception as e:  # network/HTTP error → orderly error result, no traceback
            return fail(Status.ERROR, f"{type(e).__name__}: {e}")
        try:
            clean_text = self._extract_text(body, ctype)
        except ValueError as e:
            return fail(Status.ERROR, str(e), ctype)
        # Scan BOTH views: the raw markup (hidden-CSS, <system> markers and JS payloads
        # only exist there) and the cleaned text the agent actually sees (where entity /
        # tag-split / invisible-character obfuscation has collapsed into the real phrase).
        findings = _dedupe(([] if ctype == "application/pdf" else scan(decode_body(body))) + scan(clean_text))
        result = FetchResult(
            url=url,
            status=_verdict(findings),
            findings=findings,
            fetched_at=config.utcnow().isoformat(),
            content_type=ctype or None,
            snippet=clean_text[:_SNIPPET_LEN],
            domain=domain,
            found_in=found_in,
        )
        self._write(result, clean_text)
        self._ledger(result, search_query, requested_by)
        return result


def _dedupe(findings: list[ScanFinding]) -> list[ScanFinding]:
    """Drop duplicate findings (same rule + snippet), keeping order."""
    return list(dict.fromkeys(findings))


def results_to_pending(results: Iterable[FetchResult]) -> list[PendingURL]:
    """``not_fetched``-Ergebnisse → ``PendingURL``-Liste (für :func:`bundle_access`).

    Nur URLs, die weder automatisch freigegeben noch abgerufen wurden, landen im
    gebündelten ``access``-Request (ADR 0060 §3). ``found_in`` = Provenance-Quelle,
    ``why`` = der Nicht-Freigabe-Grund.
    """
    out: list[PendingURL] = []
    for r in results:
        if r.status != Status.NOT_FETCHED:
            continue
        out.append(PendingURL(url=r.url, domain=r.domain, found_in=r.found_in, scan="", why=r.reason or ""))
    return out
