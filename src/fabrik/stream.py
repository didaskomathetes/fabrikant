"""Live-Stream eines Runs per Server-Sent Events (``GET /api/run/<id>/stream``).

Das Run-Log (``runs/<projekt>/<id>.log``) wird tail-artig gelesen und in
vollständigen Zeilen gepusht; Statuswechsel kommen aus dem Run-Record. Jedes
``log``-Event trägt als ``id`` den Byte-Offset *hinter* der Zeile — ein Reconnect
mit ``Last-Event-ID`` setzt exakt dort fort (keine Duplikate). Jeder Payload
läuft durch ``secrets._redact``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from . import secrets
from .i18n import t
from .runs import get_run, get_run_log_path
from .secrets import _redact

POLL_INTERVAL_S = 0.5  # Log-/Status-Polling serverseitig (Ziel: < 2 s Latenz)
HEARTBEAT_S = 15.0  # SSE-Kommentar, hält Proxys offen und erkennt tote Clients
CHUNK_BYTES = 64 * 1024  # max. Bytes pro log-Event (große Logs kommen in Häppchen)
READ_BYTES = 1024 * 1024  # Lesefenster je Poll (Speicher/Redaction bleiben bounded)
ACTIVE_STATUSES = frozenset({"running", "queued", "needs_approval"})


def parse_offset(raw: str | None) -> int:
    """``Last-Event-ID``/``offset`` → Byte-Offset; ungültig oder negativ → 0."""
    try:
        return max(0, int((raw or "").strip()))
    except ValueError:
        return 0


def sse(event: str, data: object, event_id: int | None = None) -> str:
    """Ein SSE-Event (JSON-Payload, einzeilig) als Wire-Text."""
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _redact_with_offsets(text: str, offset: int) -> tuple[str, list[int]]:
    """Redact text while retaining the source byte offset of each output char.

    Payload und Offsets kommen aus dem SELBEN Durchlauf, damit ihre Längen nie
    auseinanderlaufen können (eine Divergenz würde die für den SSE-Reconnect
    verwendeten Byte-Offsets korrigieren und Daten verlieren). Das Ergebnis wird
    gegen den Repo-Helfer ``secrets._redact`` geprüft, der weiterhin die
    Payload-Quelle der Wahrheit ist: weichen die beiden je auseinander, schlägt
    das hier laut fehl, statt die falschen Bytes zu maskieren.
    """
    original = text
    chars = list(text)
    boundaries: list[int] = []
    source_offset = offset
    for char in text:
        source_offset += len(char.encode("utf-8"))
        boundaries.append(source_offset)
    for secret in secrets._secrets():
        if not secret:
            continue
        redacted: list[str] = []
        redacted_boundaries: list[int] = []
        start = 0
        while True:
            found = text.find(secret, start)
            if found < 0:
                redacted.extend(chars[start:])
                redacted_boundaries.extend(boundaries[start:])
                break
            redacted.extend(chars[start:found])
            redacted_boundaries.extend(boundaries[start:found])
            end = found + len(secret)
            redacted.extend(secrets.REDACTED)
            redacted_boundaries.extend([boundaries[end - 1]] * len(secrets.REDACTED))
            start = end
            text = "".join(chars)
        chars, boundaries = redacted, redacted_boundaries
        text = "".join(chars)
    redacted_text = "".join(chars)
    # Gleicher Durchlauf wie `boundaries` → Längen stimmen immer überein.
    # Cross-Check gegen den Repo-Helfer, damit eine Änderung an `_redact`
    # hier laut auffällt, statt die Offsets still zu verschieben.
    assert redacted_text == _redact(original), t("stream.stream_redaction_deviates_from_secrets")
    return redacted_text, boundaries


def _read_lines(path: Path, offset: int, final: bool) -> bytes:
    """Bytes ab ``offset`` bis zur letzten Zeilengrenze im (bounded) Lesefenster.

    Am Dateiende mit ``final`` auch die unvollständige Schlusszeile; ohne
    Zeilengrenze im Fenster wird in ``READ_BYTES``-Schritten weitergelesen."""
    buf = bytearray()
    with path.open("rb") as f:
        f.seek(offset)
        while True:
            block = f.read(READ_BYTES)
            eof = len(block) < READ_BYTES
            scan_from = len(buf)
            buf += block
            cut = buf.rfind(b"\n", scan_from) + 1
            if cut:
                return bytes(buf) if final and eof else bytes(buf[:cut])
            if eof:
                return bytes(buf) if final else b""


def _read_chunk_with_offsets(path: Path | None, offset: int, final: bool = False) -> tuple[str, int, list[int]]:
    """Ab ``offset`` bis zur letzten vollständigen Zeile lesen → (redigierter Text, neuer Offset).

    Liest höchstens ``READ_BYTES`` je Aufruf (Rest folgt im nächsten Aufruf) und
    schneidet an einer Zeilengrenze; nur eine einzelne Zeile, die länger als das
    Fenster ist, vergrößert es. Redigiert wird der gelesene Bereich in einem
    Durchlauf, ein Secret (einzeilig) kann also nicht über Fenster hinweg
    unmaskiert bleiben. Unvollständige Schlusszeilen bleiben liegen, außer ``final``."""
    if path is None:
        return "", offset, []
    try:
        raw = _read_lines(path, offset, final)
    except OSError:
        return "", offset, []
    if not raw:
        return "", offset, []
    text, boundaries = _redact_with_offsets(raw.decode("utf-8", errors="replace"), offset)
    return text, offset + len(raw), boundaries


def read_chunk(path: Path | None, offset: int, final: bool = False) -> tuple[str, int]:
    """Ab ``offset`` bis zur letzten vollständigen Zeile lesen → (Text, Byte-Offset)."""
    text, new_offset, _ = _read_chunk_with_offsets(path, offset, final)
    return text, new_offset


def _status(run_id: str, project: str | None) -> str | None:
    try:
        rec = get_run(run_id, project)
    except (OSError, ValueError):
        return None
    return rec.get("status") if rec else None


def iter_run_events(
    run_id: str,
    project: str | None = None,
    offset: int = 0,
    *,
    poll: float = POLL_INTERVAL_S,
    heartbeat: float = HEARTBEAT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[str]:
    """SSE-Events eines Runs: ``log`` (id = Offset), ``status``, am Ende ``end``.

    Endet nach dem letzten Log-Rest, sobald der Run nicht mehr aktiv ist (oder gar
    nicht existiert). Der Status wird *vor* dem Log gelesen, damit der Drain nach
    einem Terminalstatus nichts verpasst."""
    last_status: str | None = None
    last_sent = clock()
    while True:
        status = _status(run_id, project)
        done = status not in ACTIVE_STATUSES
        if status != last_status and status is not None:
            last_status = status
            yield sse("status", {"status": status})
            last_sent = clock()
        while True:  # Log leerlesen (in Chunks)
            path = get_run_log_path(run_id, project)
            text, new_offset, boundaries = _read_chunk_with_offsets(path, offset, final=done)
            if new_offset == offset:
                break
            offset = new_offset
            # Redaction happened before splitting, so a secret cannot cross an
            # SSE payload boundary unmasked. IDs remain offsets in the original
            # UTF-8 log, not positions in the redacted payload.
            start = 0
            while start < len(text):
                end = min(start + CHUNK_BYTES, len(text))
                # Never split a replacement: all of its characters share the
                # same source offset, and reconnecting inside it would lose data.
                while end < len(text) and boundaries[end - 1] == boundaries[end]:
                    end += 1
                part = text[start:end]
                part_offset = boundaries[end - 1]
                yield sse("log", {"text": part}, event_id=part_offset)
                last_sent = clock()
                start = end
        if done:
            yield sse("end", {"status": status})
            return
        if clock() - last_sent >= heartbeat:
            yield ": keepalive\n\n"
            last_sent = clock()
        sleep(poll)
