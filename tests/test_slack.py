"""on_slack: Match/Kein-Match, Dedup, Channel-Whitelist, Validierung, Scheduler-Anbindung (ADR 0040)."""

from __future__ import annotations

import io
import json

import pytest

from fabrikant import scheduler, slack


class FakeSlack:
    """Fake-Client wie Slack: oldest < ts < latest, neueste zuerst, `page_size` Nachrichten pro Seite."""

    def __init__(self, data: dict[str, list[dict]], page_size: int = 1000):
        self.data = data
        self.page_size = page_size
        self.asked: list[str] = []

    def history(self, channel, oldest, *, latest=None, max_pages):
        self.asked.append(channel)
        if channel == "CBAD":
            raise slack.SlackError("channel_not_found")
        hi = float(latest) if latest else float("inf")
        msgs = [m for m in self.data.get(channel, []) if float(oldest) < float(m["ts"]) < hi]
        msgs.sort(key=lambda m: float(m["ts"]), reverse=True)
        limit = self.page_size * max_pages
        return slack.History(messages=msgs[:limit], complete=len(msgs) <= limit)


def _trig(**kw):
    return {"type": "on_slack", "token_env": "T", "channels": ["C1"], "pattern": r"^ALERT", **kw}


def _msg(ts, text="ALERT disk full", **kw):
    return {"ts": ts, "text": text, **kw}


@pytest.fixture
def state(tmp_path):
    return tmp_path / "slack.json"


def test_first_poll_only_sets_cursor(state):
    c = FakeSlack({"C1": [_msg("200.0")]})
    assert slack.poll(c, _trig(), state, "100.0") == []
    assert c.asked == []  # kein Backlog-Abruf


def test_match_and_variables(state):
    c = FakeSlack({"C1": [_msg("200.0")]})
    slack.poll(c, _trig(), state, "100.0")
    [a] = slack.poll(c, _trig(), state, "300.0")
    assert a.variables() == {"alert_text": "ALERT disk full", "alert_channel": "C1", "alert_ts": "200.0"}
    assert a.trigger == "on_slack:C1:200.0"


def test_no_match_and_foreign_subtypes_ignored(state):
    c = FakeSlack(
        {
            "C1": [
                _msg("200.0", "hello"),
                _msg("201.0", subtype="message_changed"),
                _msg("202.0", "ALERT x", subtype="channel_join"),
            ]
        }
    )
    slack.poll(c, _trig(), state, "100.0")
    assert slack.poll(c, _trig(), state, "300.0") == []


def test_bot_message_and_no_pattern(state):
    c = FakeSlack({"C1": [_msg("200.0", "anything", subtype="bot_message")]})
    trig = _trig()
    del trig["pattern"]
    slack.poll(c, trig, state, "100.0")
    assert len(slack.poll(c, trig, state, "300.0")) == 1


def test_double_delivery_yields_one_run(state):
    msgs = [_msg("200.0"), _msg("200.0")]  # Doppelzustellung im selben Batch
    c = FakeSlack({"C1": msgs})
    slack.poll(c, _trig(), state, "100.0")
    assert len(slack.poll(c, _trig(), state, "300.0")) == 1
    # Redelivery nach Cursor-Verlust (State ohne Cursor-Fortschritt) bleibt dank `seen` einmalig
    saved = json.loads(state.read_text())
    saved["C1"]["cursor"] = "100.0"
    state.write_text(json.dumps(saved))
    assert slack.poll(c, _trig(), state, "400.0") == []


def test_oldest_first_and_cursor_advances(state):
    c = FakeSlack({"C1": [_msg("202.0"), _msg("201.0")]})
    slack.poll(c, _trig(), state, "100.0")
    assert [a.ts for a in slack.poll(c, _trig(), state, "300.0")] == ["201.0", "202.0"]
    assert slack.poll(c, _trig(), state, "400.0") == []


def test_only_whitelisted_channels_are_read(state):
    c = FakeSlack({"C1": [], "C2": [_msg("200.0")]})
    slack.poll(c, _trig(), state, "100.0")
    assert slack.poll(c, _trig(), state, "300.0") == []
    assert set(c.asked) == {"C1"}


def test_channel_error_does_not_block_others(state, capsys):
    trig = _trig(channels=["CBAD", "C1"])
    c = FakeSlack({"C1": [_msg("200.0")]})
    slack.poll(c, trig, state, "100.0")
    assert [a.channel for a in slack.poll(c, trig, state, "300.0")] == ["C1"]
    assert "channel_not_found" in capsys.readouterr().out


def test_corrupt_state_is_reset(state):
    state.write_text("nope")
    assert slack.poll(FakeSlack({}), _trig(), state, "100.0") == []
    state.write_text("[]")
    assert slack.poll(FakeSlack({}), _trig(), state, "100.0") == []


def test_seen_is_bounded(state):
    c = FakeSlack({"C1": [_msg(f"{200 + i}.0") for i in range(slack.SEEN_MAX + 5)]})
    slack.poll(c, _trig(), state, "100.0")
    slack.poll(c, _trig(), state, "999.0")
    assert len(json.loads(state.read_text())["C1"]["seen"]) == slack.SEEN_MAX


def test_state_never_contains_token(state, monkeypatch):
    monkeypatch.setenv("T", "xoxb-secret")
    c = FakeSlack({"C1": [_msg("200.0")]})
    slack.poll(c, _trig(), state, "100.0")
    slack.poll(c, _trig(), state, "300.0")
    assert "xoxb-secret" not in state.read_text()


def test_validate_trigger():
    assert slack.validate_trigger(_trig(), "w") == []
    errs = slack.validate_trigger({"type": "on_slack", "token": "xoxb-1", "pattern": "("}, "w")
    text = " ".join(errs)
    assert "token gehört nicht" in text and "token_env" in text and "channels" in text and "Regex" in text
    assert slack.validate_trigger(_trig(pattern=5), "w")
    assert slack.validate_trigger(_trig(channels=[""]), "w")
    assert slack.validate_trigger(_trig(channels=["../etc"]), "w")  # nur Slack-IDs (Großbuchstaben/Ziffern)


def test_spec_validation_accepts_on_slack(projects_dir, tmp_path):
    from fabrikant.specs import load_projects

    (projects_dir / "p.fabrikant.yaml").write_text(
        f"name: p\nroot: {tmp_path}\nloops:\n  l:\n    triggers:\n      - {{type: on_slack, token_env: T, channels: [C1]}}\n"
        '    steps:\n      - {type: shell, cmd: "true"}\n'
    )
    assert not load_projects()["p"].get("_error")


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("T", "tok")
    assert slack.token_from_env({"token_env": "T"}) == "tok"
    assert slack.token_from_env({}) == ""


def test_web_api_client(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout):
        seen["auth"], seen["url"] = req.get_header("Authorization"), req.full_url
        return Resp(json.dumps({"ok": True, "messages": [{"ts": "1.0"}]}).encode())

    monkeypatch.setattr(slack.urllib.request, "urlopen", fake_open)
    assert slack.WebApiClient("tok").history("C1", "0", latest=None, max_pages=1).messages == [{"ts": "1.0"}]
    assert seen["auth"] == "Bearer tok" and "channel=C1" in seen["url"] and "tok" not in seen["url"]

    monkeypatch.setattr(
        slack.urllib.request, "urlopen", lambda req, timeout: Resp(b'{"ok": false, "error": "invalid_auth"}')
    )
    with pytest.raises(slack.SlackError, match="invalid_auth"):
        slack.WebApiClient("tok").history("C1", "0", latest=None, max_pages=1)


def test_web_api_client_follows_pagination(monkeypatch):
    urls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    pages = {
        "": {"ok": True, "messages": [{"ts": "2.0"}], "response_metadata": {"next_cursor": "page-2"}},
        "page-2": {"ok": True, "messages": [{"ts": "1.0"}], "response_metadata": {"next_cursor": ""}},
    }

    def fake_open(req, timeout):
        urls.append(req.full_url)
        cursor = req.full_url.split("cursor=", 1)[-1] if "cursor=" in req.full_url else ""
        return Resp(json.dumps(pages[cursor]).encode())

    monkeypatch.setattr(slack.urllib.request, "urlopen", fake_open)
    result = slack.WebApiClient("tok").history("C1", "0", latest=None, max_pages=5)
    assert [m["ts"] for m in result.messages] == ["2.0", "1.0"] and result.complete
    assert len(urls) == 2 and "cursor=page-2" in urls[1]


# ---------------------------------------------------------------- Scheduler
class _Sched(scheduler.Scheduler):
    """Echter Scheduler (nicht gestartet) mit kurzem Aufruf für `_check_slack`."""

    def check(self, trig, client=None):
        self._check_slack("p", "l", trig, client)


def test_scheduler_starts_run_via_safe_run(monkeypatch):
    fired = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda n, ln, t, **kw: fired.append((n, ln, t, kw)))
    c = FakeSlack({"C1": [_msg("200.0")]})
    trig = _trig(poll=0)
    s = _Sched()
    monkeypatch.setattr(scheduler.time, "time", lambda: 100.0)
    s.check(trig, c)  # initialisiert Cursor
    monkeypatch.setattr(scheduler.time, "time", lambda: 300.0)
    s.check(trig, c)
    s.check(trig, c)  # Doppelzustellung/Re-Poll → kein zweiter Run
    assert len(fired) == 1
    assert fired[0][:3] == ("p", "l", "on_slack:C1:200.0")
    assert fired[0][3]["initial_vars"] == {
        "alert_text": "ALERT disk full",
        "alert_channel": "C1",
        "alert_ts": "200.0",
    }
    assert "on_claimed" in fired[0][3] and "on_rejected" in fired[0][3]


def test_scheduler_retries_when_run_is_rejected(monkeypatch):
    attempts = []

    def reject(*args, **kwargs):
        attempts.append(args[2])
        kwargs["on_rejected"]()

    monkeypatch.setattr(scheduler, "_safe_run", reject)
    c = FakeSlack({"C1": [_msg("200.0")]})
    s = _Sched()
    monkeypatch.setattr(scheduler.time, "time", lambda: 100.0)
    s.check(_trig(poll=0), c)
    monkeypatch.setattr(scheduler.time, "time", lambda: 300.0)
    s.check(_trig(poll=0), c)
    s.check(_trig(poll=0), c)
    assert attempts == ["on_slack:C1:200.0", "on_slack:C1:200.0"]


def test_scheduler_claimed_alert_is_not_restarted(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "_safe_run", lambda *a, **kw: calls.append(kw))
    c = FakeSlack({"C1": [_msg("200.0")]})
    s = _Sched()
    monkeypatch.setattr(scheduler.time, "time", lambda: 100.0)
    s.check(_trig(poll=0), c)
    monkeypatch.setattr(scheduler.time, "time", lambda: 300.0)
    s.check(_trig(poll=0), c)
    calls[0]["on_claimed"]()  # Worker hat den Run geclaimt
    s.check(_trig(poll=0), c)
    assert len(calls) == 1 and not s._slack_pending
    assert json.loads(slack.state_path("p", "l").read_text())["C1"]["seen"] == ["200.0"]


def test_scheduler_poll_interval_and_missing_token(monkeypatch):
    monkeypatch.delenv("T", raising=False)
    built = []
    monkeypatch.setattr(slack, "WebApiClient", lambda tok: built.append(tok))
    s = _Sched()
    s.check(_trig())
    assert built == []  # kein Token → kein Client
    monkeypatch.setenv("T", "tok")
    s.check(_trig())  # innerhalb des Poll-Intervalls
    assert built == []


def test_scheduler_poll_failure_is_logged_without_details(monkeypatch, capsys):
    monkeypatch.setattr(scheduler, "_safe_run", lambda *a, **k: pytest.fail("kein Run"))
    monkeypatch.setattr(slack, "poll", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("xoxb-secret")))
    _Sched().check(_trig(poll=0), FakeSlack({}))
    out = capsys.readouterr().out
    assert "fehlgeschlagen" in out and "xoxb-secret" not in out


def test_invalid_ts_and_text_are_ignored(state):
    """Untrusted Slack-Daten: nur wohlgeformte `ts` und String-Texte werden zu Alerts."""
    c = FakeSlack({"C1": [_msg("200.0"), {"ts": "1e3", "text": "ALERT"}, {"ts": "201.0", "text": ["ALERT"]}]})
    c.history = lambda channel, oldest, **_: slack.History(c.data[channel])  # ohne ts-Filter (ungültige ts)
    slack.poll(c, _trig(), state, "100.0")
    assert [a.ts for a in slack.poll(c, _trig(), state, "300.0")] == ["200.0"]


def test_corrupt_cursor_resets_without_backlog(state):
    state.write_text(json.dumps({"C1": {"cursor": "nope", "seen": "x"}}))
    c = FakeSlack({"C1": [_msg("200.0")]})
    assert slack.poll(c, _trig(), state, "300.0") == []
    assert json.loads(state.read_text())["C1"] == {"cursor": "300.0", "seen": [], "pending": []}


def test_pending_survives_concurrent_mark_started(state):
    """mark_started während des Slack-Abrufs (Worker-Thread) geht nicht verloren."""
    first = FakeSlack({"C1": [_msg("200.0")]})
    slack.poll(first, _trig(), state, "100.0")
    [alert] = slack.poll(first, _trig(), state, "300.0", include_pending=True, defer_seen=True)

    class Racing(FakeSlack):
        def history(self, channel, oldest, **_):
            slack.mark_started(state, alert)  # läuft zwischen Laden und Speichern des Polls
            return slack.History()

    # Bereits gestartet → nicht erneut geliefert, und der Poll überschreibt das Update nicht.
    assert slack.poll(Racing({}), _trig(), state, "400.0", include_pending=True, defer_seen=True) == []
    saved = json.loads(state.read_text())["C1"]
    assert saved["pending"] == [] and saved["seen"] == ["200.0"]


def test_alert_vars_are_reserved():
    from fabrikant.spec_expand import RESERVED_VARS

    assert set(slack.ALERT_VARS) <= RESERVED_VARS


@pytest.mark.parametrize("poll", ["abc", float("nan"), float("inf"), -1, True, [60]])
def test_spec_rejects_invalid_poll(projects_dir, tmp_path, poll):
    from fabrikant.specs import load_projects

    (projects_dir / "p.fabrikant.yaml").write_text(
        json.dumps(
            {
                "name": "p",
                "root": str(tmp_path),
                "loops": {
                    "l": {
                        "triggers": [{"type": "on_slack", "token_env": "T", "channels": ["C1"], "poll": poll}],
                        "steps": [{"type": "shell", "cmd": "true"}],
                    }
                },
            }
        )
    )
    assert "poll muss eine endliche Zahl" in load_projects()["p"]["_error"]


@pytest.mark.parametrize("poll", [0, 60, 1.5, "60"])
def test_spec_accepts_valid_poll(projects_dir, tmp_path, poll):
    from fabrikant.specs import load_projects

    trig = {"type": "on_issue", "repo": "o/r", "poll": poll}
    (projects_dir / "p.fabrikant.yaml").write_text(
        json.dumps(
            {
                "name": "p",
                "root": str(tmp_path),
                "loops": {"l": {"triggers": [trig], "steps": [{"type": "shell", "cmd": "true"}]}},
            }
        )
    )
    assert not load_projects()["p"].get("_error")


def test_web_api_client_stops_at_page_limit(monkeypatch):
    urls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def endless(req, timeout):
        urls.append(req.full_url)
        n = len(urls)
        body = {"ok": True, "messages": [{"ts": f"{100 - n}.0"}], "response_metadata": {"next_cursor": f"p{n}"}}
        return Resp(json.dumps(body).encode())

    monkeypatch.setattr(slack.urllib.request, "urlopen", endless)
    result = slack.WebApiClient("tok").history("C1", "0", latest="150.0", max_pages=2)
    assert len(urls) == 2 and not result.complete
    assert [m["ts"] for m in result.messages] == ["99.0", "98.0"]
    assert "latest=150.0" in urls[0]


def test_page_limit_loses_no_message(state, caplog):
    """Seitenlimit: kein Alert geht verloren, keiner doppelt — auch mit neuen Nachrichten während der Lücke."""
    data = {"C1": [_msg(f"{200 + i}.0") for i in range(5)]}
    c = FakeSlack(data, page_size=2)
    trig = _trig(max_pages=1)
    slack.poll(c, trig, state, "100.0")
    first = [a.ts for a in slack.poll(c, trig, state, "300.0")]
    assert first == ["203.0", "204.0"]  # Slack liefert die neuesten zuerst
    assert "Seitenlimit" in caplog.text
    saved = json.loads(state.read_text())["C1"]
    assert saved["cursor"] == "100.0" and saved["gap_latest"] == "203.0" and saved["gap_high"] == "204.0"
    data["C1"].append(_msg("205.0"))  # neue Nachricht während der Lücke
    second = [a.ts for a in slack.poll(c, trig, state, "400.0")]
    third = [a.ts for a in slack.poll(c, trig, state, "500.0")]
    fourth = [a.ts for a in slack.poll(c, trig, state, "600.0")]
    assert second == ["201.0", "202.0"] and third == ["200.0"] and fourth == ["205.0"]
    assert sorted(first + second + third + fourth) == [f"{200 + i}.0" for i in range(6)]
    saved = json.loads(state.read_text())["C1"]
    assert saved["cursor"] == "205.0" and "gap_latest" not in saved


def test_alert_text_is_capped(state, caplog):
    caplog.set_level("DEBUG", logger="fabrikant.slack")
    c = FakeSlack({"C1": [_msg("200.0", "ALERT " + "x" * 50)]})
    trig = _trig(max_alert_chars=10)
    slack.poll(c, trig, state, "100.0")
    [a] = slack.poll(c, trig, state, "300.0", defer_seen=True)
    assert a.text == "ALERT xxxx… [gekürzt, 56 Zeichen]"
    assert json.loads(state.read_text())["C1"]["pending"][0]["text"] == a.text
    assert "gekürzt" in caplog.text
    assert slack.cap_text("kurz", 10) == "kurz"


def test_pattern_matches_full_text_before_cap(state):
    c = FakeSlack({"C1": [_msg("200.0", "x" * 50 + " FIRING")]})
    trig = _trig(pattern="FIRING", max_alert_chars=5)
    slack.poll(c, trig, state, "100.0")
    assert len(slack.poll(c, trig, state, "300.0")) == 1


@pytest.mark.parametrize("key", ["max_pages", "max_alert_chars"])
@pytest.mark.parametrize("value", [0, -1, 1.5, "abc", True, float("nan")])
def test_spec_rejects_invalid_slack_limits(projects_dir, tmp_path, key, value):
    from fabrikant.specs import load_projects

    trig = {"type": "on_slack", "token_env": "T", "channels": ["C1"], key: value}
    (projects_dir / "p.fabrikant.yaml").write_text(
        json.dumps(
            {
                "name": "p",
                "root": str(tmp_path),
                "loops": {"l": {"triggers": [trig], "steps": [{"type": "shell", "cmd": "true"}]}},
            }
        )
    )
    assert f"{key} muss eine ganze Zahl >= 1" in load_projects()["p"]["_error"]


@pytest.mark.parametrize("value", [1, 20, "20"])
def test_slack_limits_accept_vars_strings(value):
    trig = _trig(max_pages=value, max_alert_chars=value)
    assert slack.max_pages(trig) == int(value) and slack.max_alert_chars(trig) == int(value)
    assert slack.max_pages(_trig()) == slack.config.SLACK_DEFAULT_MAX_PAGES
