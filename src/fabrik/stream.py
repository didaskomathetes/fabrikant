"""Live stream of a run via server-sent events (``GET /api/run/<id>/stream``).

The run log (``runs/<project>/<id>.log``) is read tail-like and pushed in
complete lines; status changes come from the run record. Every
``log`` event carries as ``id`` the byte offset *after* the line — a reconnect
with ``Last-Event-ID`` resumes exactly there (no duplicates). Every payload
runs through ``secrets._redact``.
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
HEARTBEAT_S = 15.0  # SSE comment, keeps proxies open and detects dead clients
CHUNK_BYTES = 64 * 1024  # max. bytes per log event (large logs come in small chunks)
READ_BYTES = 1024 * 1024  # Lesefenster je Poll (Speicher/Redaction bleiben bounded)
ACTIVE_STATUSES = frozenset({"running", "queued", "needs_approval"})


def parse_offset(raw: str | None) -> int:
    """``Last-Event-ID``/``offset`` → byte offset; invalid or negative → 0."""
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

    Payload and offsets come from the SAME pass so their lengths can never
    diverge (a divergence would shift the byte offsets used for the SSE reconnect
    and lose data). The result is checked against the repo helper ``secrets._redact``,
    which remains the payload source of truth: if the two ever diverge, this
    fails loudly instead of masking the wrong bytes.
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
    # Same pass as `boundaries` → lengths always match.
    # Cross-check against the repo helper so a change to `_redact`
    # becomes loudly visible here instead of silently shifting the offsets.
    assert redacted_text == _redact(original), t("stream.stream_redaction_deviates_from_secrets")
    return redacted_text, boundaries


def _read_lines(path: Path, offset: int, final: bool) -> bytes:
    """Bytes from ``offset`` to the last line boundary in the (bounded) read window.

    At end of file with ``final`` also the incomplete last line; without a
    line boundary in the window, reading continues in ``READ_BYTES`` steps."""
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
    """Read from ``offset`` to the last complete line → (redacted text, new offset).

    Reads at most ``READ_BYTES`` per call (the rest follows in the next call) and
    cuts at a line boundary; only a single line longer than the
    window enlarges it. The read range is redacted in one
    pass, so a secret (single-line) cannot stay unmasked across windows.
    Incomplete last lines stay unread, except ``final``."""
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
    """Read from ``offset`` to the last complete line → (text, byte offset)."""
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
    """SSE events of a run: ``log`` (id = offset), ``status``, at the end ``end``.

    Ends after the last log remainder as soon as the run is no longer active (or does not
    exist at all). The status is read *before* the log so the drain
    misses nothing after a terminal status."""
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
