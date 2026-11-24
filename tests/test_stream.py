"""SSE-Stream (stream.py), Transkript (sessionlog.transcript) und die Endpoints dazu."""

from __future__ import annotations

import http.client
import json

import pytest

from fabrikant import secrets, sessionlog, stream

RID = "20260101T000000Z-s"
SECRET = "s3cr3t-value-123456"


def _write(runs_dir, status="pass", log="", rid=RID):
    d = runs_dir / "p"
    d.mkdir(exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({"id": rid, "project": "p", "loop": "l", "status": status}))
    if log is not None:
        (d / f"{rid}.log").write_bytes(log.encode())
    return d / f"{rid}.log"


def _parse(events):
    """SSE-Wire-Text → [(event, id, data)] (Kommentare übersprungen)."""
    out = []
    for e in events:
        if e.startswith(":"):
            continue
        f = dict(line.split(": ", 1) for line in e.strip().splitlines())
        out.append((f["event"], f.get("id"), json.loads(f["data"])))
    return out


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRIKANT_SESSIONLOG", str(tmp_path / "sl" / "t.db"))
    monkeypatch.setattr(sessionlog, "_conn", None)
    yield
    monkeypatch.setattr(sessionlog, "_conn", None)


@pytest.mark.parametrize("raw,exp", [(None, 0), ("", 0), ("12", 12), (" 7 ", 7), ("-3", 0), ("x", 0)])
def test_parse_offset(raw, exp):
    assert stream.parse_offset(raw) == exp


def test_finished_run_streams_full_log_then_ends(runs_dir):
    _write(runs_dir, "pass", "a\nb\n")
    ev = _parse(stream.iter_run_events(RID, "p", sleep=lambda s: None))
    assert [e[0] for e in ev] == ["status", "log", "end"]
    assert ev[1][2]["text"] == "a\nb\n"
    assert ev[1][1] == "4"  # Byte-Offset hinter der letzten Zeile
    assert ev[2][2] == {"status": "pass"}


def test_reconnect_with_offset_has_no_duplicates(runs_dir):
    _write(runs_dir, "pass", "a\nb\nc\n")
    ev = _parse(stream.iter_run_events(RID, "p", offset=4, sleep=lambda s: None))
    assert "".join(e[2]["text"] for e in ev if e[0] == "log") == "c\n"


def test_running_run_pushes_new_lines_and_incomplete_line_waits(runs_dir):
    log = _write(runs_dir, "running", "one\npart")
    states = iter(["more\n", "FIN"])

    def sleeper(_):
        step = next(states)
        if step == "FIN":
            _write(runs_dir, "pass", log.read_text() + "ial\n")
        else:
            log.write_bytes(log.read_bytes() + step.encode())

    ev = _parse(stream.iter_run_events(RID, "p", sleep=sleeper))
    texts = [e[2]["text"] for e in ev if e[0] == "log"]
    assert texts[0] == "one\n"  # "part" ist unvollständig → zurückgehalten
    assert "".join(texts) == "one\npartmore\nial\n"
    assert [e[0] for e in ev if e[0] != "log"] == ["status", "status", "end"]


def test_final_drain_flushes_unterminated_last_line(runs_dir):
    _write(runs_dir, "fail", "no newline")
    ev = _parse(stream.iter_run_events(RID, "p", sleep=lambda s: None))
    assert [e[2]["text"] for e in ev if e[0] == "log"] == ["no newline"]


def test_unknown_run_ends_immediately(runs_dir):
    assert _parse(stream.iter_run_events("nope", "p", sleep=lambda s: None)) == [("end", None, {"status": None})]


def test_heartbeat_when_idle(runs_dir):
    _write(runs_dir, "running", "")
    t = iter(range(0, 100, 10))
    gen = stream.iter_run_events(RID, "p", heartbeat=15, sleep=lambda s: None, clock=lambda: next(t))
    got = [next(gen) for _ in range(3)]
    assert any(g.startswith(": keepalive") for g in got)


def test_large_log_is_chunked(runs_dir, monkeypatch):
    monkeypatch.setattr(stream, "CHUNK_BYTES", 10)
    _write(runs_dir, "pass", "aaaa\nbbbb\ncccc\n")
    ev = [e for e in _parse(stream.iter_run_events(RID, "p", sleep=lambda s: None)) if e[0] == "log"]
    assert len(ev) > 1
    assert "".join(e[2]["text"] for e in ev) == "aaaa\nbbbb\ncccc\n"


def test_huge_single_line_is_emitted_in_pieces(runs_dir, monkeypatch):
    monkeypatch.setattr(stream, "CHUNK_BYTES", 4)
    _write(runs_dir, "pass", "abcdefghij\n")
    ev = [e for e in _parse(stream.iter_run_events(RID, "p", sleep=lambda s: None)) if e[0] == "log"]
    assert "".join(e[2]["text"] for e in ev) == "abcdefghij\n"


def test_stream_redacts_secrets(runs_dir, monkeypatch):
    monkeypatch.setattr(secrets, "_runtime_secrets", [SECRET])
    _write(runs_dir, "pass", f"token is {SECRET}\n")
    wire = "".join(stream.iter_run_events(RID, "p", sleep=lambda s: None))
    assert SECRET not in wire
    assert "[REDACTED]" in wire


def test_redact_with_offsets_payload_and_boundaries_stay_aligned(monkeypatch):
    """Payload und Offsets kommen aus demselben Durchlauf → Längen stimmen immer."""
    monkeypatch.setattr(secrets, "_runtime_secrets", [SECRET])
    text, boundaries = stream._redact_with_offsets(f"a {SECRET} b\n", 100)
    assert len(text) == len(boundaries)
    assert SECRET not in text
    # Offsets sind Byte-Positionen HINTER dem jeweiligen Zeichen (ab offset=100).
    assert boundaries[0] == 101  # 'a'
    assert boundaries[-1] == 100 + len(f"a {SECRET} b\n".encode())


def test_redact_with_offsets_fails_loud_on_redact_divergence(monkeypatch):
    """Weicht secrets._redact ab, schlägt der Cross-Check laut fehl (keine stillen Offset-Drifts)."""
    monkeypatch.setattr(secrets, "_runtime_secrets", [SECRET])
    monkeypatch.setattr(stream, "_redact", lambda s: s.replace(SECRET, "X"))  # andere Länge
    with pytest.raises(AssertionError):
        stream._redact_with_offsets(f"a {SECRET} b\n", 0)


def test_read_is_bounded_per_call(tmp_path, monkeypatch):
    """Ein großer Log-Rest wird fensterweise gelesen, nicht komplett materialisiert."""
    monkeypatch.setattr(stream, "READ_BYTES", 8)
    log = tmp_path / "x.log"
    log.write_bytes(b"aaa\nbbb\nccc\nddd\n")
    text, off = stream.read_chunk(log, 0)
    assert (text, off) == ("aaa\nbbb\n", 8)
    text, off = stream.read_chunk(log, off)
    assert (text, off) == ("ccc\nddd\n", 16)
    assert stream.read_chunk(log, off) == ("", 16)


def test_read_window_grows_for_long_line(tmp_path, monkeypatch):
    monkeypatch.setattr(stream, "READ_BYTES", 4)
    log = tmp_path / "x.log"
    log.write_bytes(b"abcdefghij\nrest")
    assert stream.read_chunk(log, 0) == ("abcdefghij\n", 11)
    assert stream.read_chunk(log, 11) == ("", 11)  # unvollständige Zeile wartet
    assert stream.read_chunk(log, 11, final=True) == ("rest", 15)


def test_bounded_read_keeps_secret_masked_and_stream_complete(runs_dir, monkeypatch):
    monkeypatch.setattr(stream, "READ_BYTES", 8)
    monkeypatch.setattr(secrets, "_runtime_secrets", [SECRET])
    log = "x\n" * 10 + f"token {SECRET} end\n" + "y\n" * 10 + "tail"
    _write(runs_dir, "pass", log)
    ev = _parse(stream.iter_run_events(RID, "p", sleep=lambda s: None))
    text = "".join(e[2]["text"] for e in ev if e[0] == "log")
    assert text == log.replace(SECRET, secrets.REDACTED)
    assert [e[1] for e in ev if e[0] == "log"][-1] == str(len(log.encode()))


def test_read_chunk_missing_file(tmp_path):
    assert stream.read_chunk(None, 5) == ("", 5)
    assert stream.read_chunk(tmp_path / "x.log", 0) == ("", 0)


# ---------------------------------------------------------------- Transkript
def test_transcript_roles_and_pagination(fresh_db):
    lg = sessionlog.SessionLog(RID, "p", "l")
    lg.start()
    lg.event("prompt", step_name="dev", step_type="agent", text="do it")
    lg.event("tool_call", step_name="dev", step_type="agent", tool="bash")
    lg.event("message", step_name="dev", step_type="agent", text=f"done {SECRET}")
    full = sessionlog.transcript(RID)
    assert [e.role for e in full] == ["user", "tool", "assistant"]
    assert full[1].tool == "bash" and full[0].text == "do it"
    page = sessionlog.transcript(RID, after=full[0].id, limit=1)
    assert [e.role for e in page] == ["tool"]
    assert sessionlog.transcript("other") == []


def test_transcript_db_error_returns_empty(fresh_db, monkeypatch):
    def boom(*a, **k):
        raise sessionlog.sqlite3.Error("x")

    monkeypatch.setattr(sessionlog, "connect", boom)
    assert sessionlog.transcript(RID) == []


# ---------------------------------------------------------------- HTTP
@pytest.fixture
def base(runs_dir, clean_token, live_server):
    return live_server()


def _get(base, path, headers=None):
    port = int(base.rsplit(":", 1)[1])
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, r.getheader("Content-Type"), r.read().decode()


def test_stream_endpoint_ends_at_run_end_and_redacts(base, runs_dir, monkeypatch):
    monkeypatch.setattr(secrets, "_runtime_secrets", [SECRET])
    _write(runs_dir, "pass", f"hello {SECRET}\nbye\n")
    status, ctype, body = _get(base, f"/api/run/{RID}/stream?project=p")
    assert status == 200 and ctype.startswith("text/event-stream")
    assert SECRET not in body and "event: end" in body
    # Reconnect via Last-Event-ID: nur der Rest
    off = len(f"hello {SECRET}\n".encode())
    _, _, rest = _get(base, f"/api/run/{RID}/stream?project=p", {"Last-Event-ID": str(off)})
    assert "bye" in rest and "hello" not in rest


def test_stream_endpoint_unknown_run_404(base):
    assert _get(base, "/api/run/nope/stream")[0] == 404


def test_transcript_endpoint(base, runs_dir, fresh_db):
    _write(runs_dir, "pass", "x\n")
    lg = sessionlog.SessionLog(RID, "p", "l")
    lg.start()
    lg.event("message", step_name="dev", text="hi")
    status, _, body = _get(base, f"/api/run/{RID}/transcript?project=p")
    data = json.loads(body)
    assert status == 200 and data["entries"][0]["text"] == "hi"
    assert data["next_after"] == data["entries"][-1]["id"]
    assert _get(base, f"/api/run/{RID}/transcript?after=x")[0] == 400
    assert _get(base, f"/api/run/{RID}/transcript?limit=0")[0] == 400
    assert _get(base, "/api/run/nope/transcript")[0] == 404
