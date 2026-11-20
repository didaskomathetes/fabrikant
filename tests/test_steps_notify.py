"""Step: notify — _notify (Alerts laut Spec) + _step_notify (Step)."""

from __future__ import annotations

from pathlib import Path
from urllib import parse

import pytest

from fabrik import gitops
from fabrik.hosting import get_provider
from fabrik.steps import notify


def _record(project: str = "p", loop: str = "l", status: str = "fail", rid: str = "r1") -> dict:
    return {"project": project, "loop": loop, "status": status, "id": rid, "trigger": "test"}


# ---------------------------------------------------------------- _notify
def test_notify_no_config() -> None:
    assert notify._notify({}, _record()) == []


def test_notify_event_not_configured() -> None:
    # status=fail → ev=run_failed, aber events=run_passed → kein Fire
    spec = {"notify": {"events": ["run_passed"], "slack": "https://x"}}
    assert notify._notify(spec, _record(status="fail")) == []


def test_notify_no_matching_event() -> None:
    # status=pass → ev=run_passed, events=run_failed → kein Fire
    spec = {"notify": {"events": ["run_failed"], "slack": "https://x"}}
    assert notify._notify(spec, _record(status="pass")) == []


def test_notify_slack_and_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _post(url: str, payload: dict) -> tuple[int, str]:
        calls.append(url)
        return (200, "ok")

    monkeypatch.setattr(notify, "_http_post", _post)
    spec = {
        "notify": {
            "events": ["run_failed"],
            "slack": "https://hooks.slack.com/x",
            "webhooks": [{"name": "dd", "url": "https://dd/x"}],
        }
    }
    sent = notify._notify(spec, _record(status="fail"))
    assert "slack:200" in sent
    assert "dd:200" in sent
    assert len(calls) == 2


def test_notify_slack_dict_form(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def _post(url, payload):
        seen["url"] = url
        return (201, "ok")

    monkeypatch.setattr(notify, "_http_post", _post)
    spec = {"notify": {"events": ["run_failed"], "slack": {"url": "https://hooks/x"}}}
    sent = notify._notify(spec, _record(status="fail"))
    assert seen["url"] == "https://hooks/x"
    assert "slack:201" in sent


def test_notify_github_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "_http_post", lambda u, p: (200, "ok"))
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", lambda m, u, t, payload=None, accept=None: {"number": 42})
    spec = {
        "notify": {"events": ["run_failed"], "github_issue": True, "issue_labels": ["fabrik"]},
        "github": "o/r",
    }
    sent = notify._notify(spec, _record(status="error"))
    assert "issue#42" in sent


def test_notify_github_issue_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "_http_post", lambda u, p: (200, "ok"))
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")

    def _boom(m, u, t, payload=None, accept=None):
        raise RuntimeError("gh down")

    monkeypatch.setattr(gitops, "_gh_api", _boom)
    spec = {"notify": {"events": ["run_failed"], "github_issue": True}, "github": "o/r"}
    sent = notify._notify(spec, _record(status="fail"))
    assert any(s.startswith("issue:error") for s in sent)


def test_notify_github_issue_programming_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Programmierfehler im Alert-Issue-Pfad (kein API-Fehler) propagieren —
    sie werden NICHT in einen Kanal-Eintrag verwandelt (sonst würden echte
    Bugs als scheinbar normaler Kanalstatus verschluckt)."""
    monkeypatch.setattr(notify, "_http_post", lambda u, p: (200, "ok"))

    def _bug(provider, gh: str, labels: list[str], record: dict, tail: str) -> str:
        raise TypeError("bug")

    monkeypatch.setattr(notify, "_alert_issue", _bug)
    spec = {"notify": {"events": ["run_failed"], "github_issue": True}, "github": "o/r"}
    with pytest.raises(TypeError, match="bug"):
        notify._notify(spec, _record(status="fail"))


def test_notify_github_issue_api_error_becomes_channel_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """API-/Token-/Netzwerkfehler (im _GH_API_ERRORS-Vertrag) werden weiterhin
    in einen Kanal-Eintrag `issue:error(...)` übersetzt — der Run wird nicht
    gekillt."""
    monkeypatch.setattr(notify, "_http_post", lambda u, p: (200, "ok"))

    def _api_err(provider, gh: str, labels: list[str], record: dict, tail: str) -> str:
        raise RuntimeError("gh down")

    monkeypatch.setattr(notify, "_alert_issue", _api_err)
    spec = {"notify": {"events": ["run_failed"], "github_issue": True}, "github": "o/r"}
    assert notify._notify(spec, _record(status="fail")) == ["issue:error(gh down)"]


def test_notify_github_issue_no_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "_http_post", lambda u, p: (200, "ok"))
    spec = {"notify": {"events": ["run_failed"], "github_issue": True}}  # kein github
    sent = notify._notify(spec, _record(status="fail"))
    assert sent == []


def test_notify_log_tail(runs_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Log-Datei existiert → log_tail wird gefüllt
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.log").write_text("line1\nline2\n")
    seen: dict = {}
    monkeypatch.setattr(notify, "_http_post", lambda url, payload: seen.update(payload=payload) or (200, "ok"))
    # Webhook-Payload trägt das volle Payload (inkl. log_tail); Slack packt den
    # Tail in den Text.
    spec = {"notify": {"events": ["run_failed"], "webhooks": [{"name": "w", "url": "https://x"}]}}
    notify._notify(spec, _record(status="fail"))
    assert "line2" in seen["payload"]["log_tail"]


def test_notify_log_tail_redacted(runs_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """log_tail wird vor dem Versand redigiert (ADR 0026)."""
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.log").write_text("password=supersecret\n")
    seen: dict = {}
    monkeypatch.setattr(notify, "_http_post", lambda url, payload: seen.update(payload=payload) or (200, "ok"))
    spec = {"notify": {"events": ["run_failed"], "webhooks": [{"name": "w", "url": "https://x"}]}}
    notify._notify(spec, _record(status="fail"))
    assert "supersecret" not in seen["payload"]["log_tail"]
    assert "[REDACTED]" in seen["payload"]["log_tail"]


def test_notify_log_tail_redaction_disabled(runs_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ALERT_LOG_REDACTION=False → log_tail unverändert."""
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.log").write_text("password=supersecret\n")
    seen: dict = {}
    monkeypatch.setattr(notify.config, "ALERT_LOG_REDACTION", False)
    monkeypatch.setattr(notify, "_http_post", lambda url, payload: seen.update(payload=payload) or (200, "ok"))
    spec = {"notify": {"events": ["run_failed"], "webhooks": [{"name": "w", "url": "https://x"}]}}
    notify._notify(spec, _record(status="fail"))
    assert "supersecret" in seen["payload"]["log_tail"]


# ---------------------------------------------------------------- Alert-Dedup
_COPILOT_FAIL = "agent session fehlgeschlagen: copilot: Session endete ohne finale Antwort (session copilot-{sid})"


def _failed(rid: str, sid: str = "8a1f1b454c95", summary: str = _COPILOT_FAIL, step: str = "review") -> dict:
    rec = _record(project="fabrik", loop="review-pr", status="fail", rid=rid)
    rec["steps"] = [
        {"name": "prepare", "status": "pass", "summary": "ok"},
        {"name": step, "status": "fail", "summary": summary.format(sid=sid)},
    ]
    return rec


class _FakeGitHub:
    """`_gh_api`-Double: hält offene Issues, zeichnet Calls auf."""

    def __init__(self, open_issues: list[dict] | None = None, fail_lookup: bool = False) -> None:
        self.issues = list(open_issues or [])
        self.fail_lookup = fail_lookup
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, url: str, token: str, payload: dict | None = None) -> object:
        self.calls.append((method, url, payload))
        if method == "GET":
            if self.fail_lookup:
                raise RuntimeError("gh down")
            return [i for i in self.issues if i["state"] == "open"]
        if url.endswith("/comments"):
            return {"id": 1}
        num = 100 + len(self.issues)
        assert payload is not None
        self.issues.append({"number": num, "state": "open", "body": payload["body"]})
        return {"number": num}

    def posts(self, suffix: str) -> list[tuple[str, str, dict | None]]:
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


def _gh_spec() -> dict:
    return {
        "notify": {"events": ["run_failed"], "github_issue": True, "issue_labels": ["fabrik-alert"]},
        "github": "o/r",
    }


def _patch_gh(monkeypatch: pytest.MonkeyPatch, fake: _FakeGitHub) -> None:
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", fake)


def test_alert_fingerprint_ignores_run_and_session_ids() -> None:
    a = notify._alert_fingerprint(_failed("20261019T103001Z-5cc6786ae679-review-pr", sid="8a1f1b454c95"))
    b = notify._alert_fingerprint(_failed("20261019T114000Z-e57f92914fe7-review-pr", sid="ee9f51d7ad6f"))
    assert a == b


def test_alert_fingerprint_differs_by_error_step_and_loop() -> None:
    base = notify._alert_fingerprint(_failed("r1"))
    assert notify._alert_fingerprint(_failed("r1", summary="agent timeout")) != base
    assert notify._alert_fingerprint(_failed("r1", step="lint")) != base
    other_loop = _failed("r1")
    other_loop["loop"] = "develop"
    assert notify._alert_fingerprint(other_loop) != base


def test_alert_fingerprint_keeps_meaningful_numbers() -> None:
    """Kurze Zahlen sind bedeutungsvoll (Fehleranzahl, Port) → nicht normalisieren."""
    a = notify._alert_fingerprint(_failed("r1", summary="1 error(s) in build"))
    b = notify._alert_fingerprint(_failed("r2", summary="2 error(s) in build"))
    assert a != b
    # Lange Hex-Tokens (IDs) werden weiterhin normalisiert
    c = notify._alert_fingerprint(_failed("r1", summary="failed at 8a1f1b454c95"))
    d = notify._alert_fingerprint(_failed("r2", summary="failed at ee9f51d7ad6f"))
    assert c == d


def test_alert_fingerprint_keeps_long_decimal_numbers() -> None:
    """Lange Dezimalzahlen (Timestamps, Epoch-ms) sind keine Hex-Tokens →
    erhalten bleiben (sonst würden unterschiedliche Fehler zusammenziehen)."""
    a = notify._alert_fingerprint(_failed("r1", summary="timeout after 1729345678901 ms"))
    b = notify._alert_fingerprint(_failed("r2", summary="timeout after 1729345679000 ms"))
    assert a != b
    # Hex-Tokens mit a-f werden weiterhin normalisiert
    c = notify._alert_fingerprint(_failed("r1", summary="failed at 8a1f1b454c95"))
    d = notify._alert_fingerprint(_failed("r2", summary="failed at ee9f51d7ad6f"))
    assert c == d


def test_alert_fingerprint_normalizes_uuids() -> None:
    a = notify._alert_fingerprint(_failed("r1", summary="session 550e8400-e29b-41d4-a716-446655440000"))
    b = notify._alert_fingerprint(_failed("r2", summary="session 6ba7b810-9dad-41d4-80b4-00c04fd430c8"))
    assert a == b


def test_alert_fingerprint_stale_run_without_failed_step() -> None:
    """Stale-Run (Dead-Man's-Switch): kein fehlgeschlagener Step → Run-Fehler."""
    rec = _record(status="error", rid="r1")
    rec["error"] = "stale: >6h in 'running' — Dead-Man's-Switch"
    rec2 = {**rec, "id": "r2"}
    assert notify._alert_fingerprint(rec) == notify._alert_fingerprint(rec2)
    assert notify._failure_reason(rec)[0] == "run"


def test_alert_dedup_comments_on_open_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (2026-10-19): 22 Alert-Issues für denselben Fehler → jetzt
    ein Issue, Folge-Runs werden kommentiert."""
    fake = _FakeGitHub()
    _patch_gh(monkeypatch, fake)
    first = notify._notify(_gh_spec(), _failed("run-a", sid="aaaaaaaaaaaa"))
    second = notify._notify(_gh_spec(), _failed("run-b", sid="bbbbbbbbbbbb"))
    assert first == ["issue#100"]
    assert second == ["issue#100(+)"]
    assert len(fake.posts("/issues")) == 1
    (comment,) = fake.posts("/issues/100/comments")
    assert comment[2] is not None
    assert "run-b" in comment[2]["body"]
    # Lookup filtert offene Issues mit den Alert-Labels
    get_url = next(c[1] for c in fake.calls if c[0] == "GET")
    assert "state=open" in get_url
    assert "labels=fabrik-alert" in get_url


def test_alert_new_issue_for_different_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGitHub()
    _patch_gh(monkeypatch, fake)
    notify._notify(_gh_spec(), _failed("run-a"))
    sent = notify._notify(_gh_spec(), _failed("run-b", summary="agent timeout nach 1800s"))
    assert sent == ["issue#101"]
    assert len(fake.posts("/issues")) == 2


def test_alert_new_issue_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Geschlossenes Issue zählt nicht — Fehler tritt wieder auf → neues Issue."""
    fake = _FakeGitHub()
    _patch_gh(monkeypatch, fake)
    notify._notify(_gh_spec(), _failed("run-a"))
    fake.issues[0]["state"] = "closed"
    assert notify._notify(_gh_spec(), _failed("run-b")) == ["issue#101"]


def test_alert_issue_body_carries_marker_and_title_without_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGitHub()
    _patch_gh(monkeypatch, fake)
    rec = _failed("20261019T114000Z-e57f92914fe7-review-pr")
    notify._notify(_gh_spec(), rec)
    (_, _, payload) = fake.posts("/issues")[0]
    assert payload is not None
    assert f"fabrik-alert:fp={notify._alert_fingerprint(rec)}" in payload["body"]
    assert payload["title"].startswith("[fabrik] fabrik/review-pr FAILED [unknown] — review:")
    assert rec["id"] not in payload["title"]
    assert payload["labels"] == ["fabrik-alert"]


def test_alert_environment_title_bare_body_highlighted(monkeypatch: pytest.MonkeyPatch) -> None:
    """`environment`: Titel trägt den kurzen Wert, der Body den hervorgehobenen Label (ADR 0035 Nr. 5)."""
    fake = _FakeGitHub()
    _patch_gh(monkeypatch, fake)
    rec = _failed("r-env")
    rec["failure_kind"] = "environment"
    notify._notify(_gh_spec(), rec)
    (_, _, payload) = fake.posts("/issues")[0]
    assert payload is not None
    assert payload["title"].startswith("[fabrik] fabrik/review-pr FAILED [environment] — review:")
    assert "braucht einen Menschen" not in payload["title"]
    assert "braucht einen Menschen" in payload["body"]


def test_alert_title_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Title: Whitespace kollabiert, Markdown/Klammern ersetzt, truncated."""
    fake = _FakeGitHub()
    _patch_gh(monkeypatch, fake)
    rec = _failed("r1", summary="line1\nline2 `code` [bracket] " + "x" * 120)
    notify._notify(_gh_spec(), rec)
    (_, _, payload) = fake.posts("/issues")[0]
    assert payload is not None
    title = payload["title"]
    prefix = "[fabrik] fabrik/review-pr FAILED [unknown] — review: "
    assert title.startswith(prefix)
    sanitized = title[len(prefix) :]
    assert "\n" not in title
    assert "`" not in sanitized and "[" not in sanitized and "]" not in sanitized
    assert len(sanitized) <= 80


def test_alert_lookup_failure_still_creates_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lookup kaputt → lieber Duplikat als verlorener Alert."""
    fake = _FakeGitHub(fail_lookup=True)
    _patch_gh(monkeypatch, fake)
    assert notify._notify(_gh_spec(), _failed("run-a")) == ["issue#100"]


def test_alert_lookup_paginates_beyond_first_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mehr offene Alert-Issues als eine Seite → Lookup paginiert weiter
    (statt das relevante Issue auf Seite 2+ zu übersehen)."""
    monkeypatch.setattr(notify.config, "ALERT_OPEN_ISSUES_PAGE", 2)
    marker = notify._ALERT_MARKER.format(fp=notify._alert_fingerprint(_failed("run-a")))
    issues = [
        {"number": 1, "state": "open", "body": "anderes Fehlerbild"},
        {"number": 2, "state": "open", "body": "auch anders"},
        {"number": 3, "state": "open", "body": f"irgendwas\n{marker}"},
    ]

    def _paged(method: str, url: str, token: str, payload: dict | None = None, accept=None) -> object:
        if method == "GET":
            qs = parse.parse_qs(parse.urlparse(url).query)
            page, per = int(qs["page"][0]), int(qs["per_page"][0])
            return issues[(page - 1) * per : page * per]
        return {"number": 100}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _paged)
    assert notify._find_open_alert(get_provider("o/r"), "o/r", ["fabrik-alert"], marker) == 3


def test_alert_lookup_caps_page_size_to_github_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """ALERT_OPEN_ISSUES_PAGE > GitHub-Limit → per_page wird gekappt; sonst
    würde die volle 100er-Seite als „letzte“ gelten und Seite 2 nie gelesen."""
    monkeypatch.setattr(notify.config, "ALERT_OPEN_ISSUES_PAGE", 500)
    monkeypatch.setattr(notify.config, "GH_MAX_PER_PAGE", 2)
    marker = notify._ALERT_MARKER.format(fp=notify._alert_fingerprint(_failed("run-a")))
    issues = [
        {"number": 1, "state": "open", "body": "anders"},
        {"number": 2, "state": "open", "body": "anders"},
        {"number": 3, "state": "open", "body": marker},
    ]

    def _capped(method: str, url: str, token: str, payload: dict | None = None, accept=None) -> object:
        qs = parse.parse_qs(parse.urlparse(url).query)
        page, per = int(qs["page"][0]), min(int(qs["per_page"][0]), 2)  # API-Kappung wie GitHub
        return issues[(page - 1) * per : page * per]

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _capped)
    assert notify._find_open_alert(get_provider("o/r"), "o/r", ["fabrik-alert"], marker) == 3


def test_alert_lookup_aborts_after_max_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rate-Limit-Schutz: nach ALERT_LOOKUP_MAX_PAGES Seiten wird der Lookup
    abgebrochen (None → Duplikat-Issue statt API-Budget-Aufbrauch)."""
    monkeypatch.setattr(notify.config, "ALERT_OPEN_ISSUES_PAGE", 2)
    monkeypatch.setattr(notify.config, "ALERT_LOOKUP_MAX_PAGES", 2)
    calls = 0

    def _infinite(method: str, url: str, token: str, payload: dict | None = None, accept=None) -> object:
        nonlocal calls
        if method == "GET":
            calls += 1
            return [{"number": calls * 10, "state": "open", "body": "kein Match"}] * 2
        return {"number": 100}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _infinite)
    assert notify._find_open_alert(get_provider("o/r"), "o/r", ["fabrik-alert"], "marker") is None
    assert calls == 2


def test_alert_lookup_skips_pull_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub-Issue-Liste enthält auch PRs (Feld `pull_request`) → werden
    übersprungen; nur echte Issues werden auf den Marker geprüft."""
    marker = notify._ALERT_MARKER.format(fp=notify._alert_fingerprint(_failed("run-a")))
    issues = [
        {"number": 1, "state": "open", "body": f"PR-Body mit {marker}", "pull_request": {"url": "x"}},
        {"number": 2, "state": "open", "body": f"Issue-Body mit {marker}"},
    ]

    def _gh(method: str, url: str, token: str, payload: dict | None = None, accept=None) -> object:
        return issues

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _gh)
    assert notify._find_open_alert(get_provider("o/r"), "o/r", ["fabrik-alert"], marker) == 2


def test_alert_fingerprint_normalizes_ids_to_named_token() -> None:
    """Normalisierte IDs werden durch das benannte Token ersetzt (nicht '#')."""
    norm = notify._VOLATILE.sub(notify._VOLATILE_REPLACEMENT, "Fehler in 8a1f1b454c95 und 123e45678901")
    assert notify._VOLATILE_REPLACEMENT in norm
    assert "#" not in norm


def test_alert_fingerprint_is_16_hex_chars() -> None:
    """Fingerprint-Länge 16 (64 Bit) → Kollisionsrisiko vernachlässigbar."""
    fp = notify._alert_fingerprint(_failed("r1"))
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_alert_fingerprint_hex_token_vs_decimal_token() -> None:
    """Grenzfälle der _VOLATILE-Regex: Hex-Token (mind. ein a-f, ≥ 8 Zeichen)
    wird normalisiert, reine Dezimalzahl (auch lang) bleibt erhalten."""
    # Hex (mit a-f) → beide normalisiert → gleicher Fingerprint
    hex_a = notify._alert_fingerprint(_failed("r1", summary="id 8a1f1b454c95"))
    hex_b = notify._alert_fingerprint(_failed("r2", summary="id ee9f51d7ad6f"))
    assert hex_a == hex_b
    # Reine Dezimalzahl (kein a-f) → bleibt erhalten → unterschiedlich
    dec_a = notify._alert_fingerprint(_failed("r1", summary="id 123456789012"))
    dec_b = notify._alert_fingerprint(_failed("r2", summary="id 123456789013"))
    assert dec_a != dec_b


def test_alert_fingerprint_embedded_id_in_longer_string() -> None:
    """Eingebettete IDs (z. B. `copilot-8a1f1b454c95`) werden normalisiert,
    der umgebende Text bleibt erhalten."""
    a = notify._alert_fingerprint(_failed("r1", summary="session copilot-8a1f1b454c95 ended"))
    b = notify._alert_fingerprint(_failed("r2", summary="session copilot-ee9f51d7ad6f ended"))
    assert a == b


def test_alert_fingerprint_no_over_eager_normalization() -> None:
    """Kein Zusammenziehen unterschiedlicher Fehlerbilder: kurze,
    bedeutungsvolle Zahlen (Ports, Fehleranzahl) bleiben erhalten."""
    a = notify._alert_fingerprint(_failed("r1", summary="connect to port 8080 refused"))
    b = notify._alert_fingerprint(_failed("r2", summary="connect to port 8081 refused"))
    assert a != b


def test_alert_dedup_with_multiple_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mehrere issue_labels: Lookup filtert mit allen Labels, Dedup funktioniert."""
    fake = _FakeGitHub()
    _patch_gh(monkeypatch, fake)
    spec = _gh_spec()
    spec["notify"]["issue_labels"] = ["fabrik-alert", "team-x"]
    first = notify._notify(spec, _failed("run-a"))
    second = notify._notify(spec, _failed("run-b", sid="bbbbbbbbbbbb"))
    assert first == ["issue#100"]
    assert second == ["issue#100(+)"]
    get_url = next(c[1] for c in fake.calls if c[0] == "GET")
    assert "labels=fabrik-alert%2Cteam-x" in get_url
    (_, _, payload) = fake.posts("/issues")[0]
    assert payload is not None
    assert payload["labels"] == ["fabrik-alert", "team-x"]


def test_alert_lookup_labels_with_special_chars_are_percent_encoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Labels mit Sonderzeichen (Leerzeichen, +, #) → percent-encodiert in der
    Query; dekodiert ergibt sich wieder der Originalwert (GitHub akzeptiert
    percent-encoding — urlencode ist hier sicher)."""
    fake = _FakeGitHub()
    _patch_gh(monkeypatch, fake)
    spec = _gh_spec()
    spec["notify"]["issue_labels"] = ["team x", "a+b", "c#d"]
    notify._notify(spec, _failed("run-a"))
    get_url = next(c[1] for c in fake.calls if c[0] == "GET")
    qs = parse.parse_qs(parse.urlparse(get_url).query)
    assert qs["labels"] == ["team x,a+b,c#d"]


def test_alert_post_without_number_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST-Antwort ohne 'number' (Shape-Change/Fehler-Body) → klarer Fehler, kein Crash."""
    fake = _FakeGitHub()
    _patch_gh(monkeypatch, fake)

    def _gh_api_no_number(method: str, url: str, token: str, payload: dict | None = None, accept=None) -> object:
        if method == "GET":
            return []
        return {"message": "Validation Failed"}

    monkeypatch.setattr(gitops, "_gh_api", _gh_api_no_number)
    assert notify._notify(_gh_spec(), _failed("run-a")) == ["issue:error(keine Issue-Nummer in API-Antwort)"]


def test_alert_lookup_unexpected_shape_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unerwartete API-Antwort (dict statt list) → Provider normalisiert zu leerer
    Liste → Lookup liefert None (kein Crash)."""

    def _dict_response(method: str, url: str, token: str, payload: dict | None = None, accept=None) -> object:
        return {"message": "rate limited"}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _dict_response)
    assert notify._find_open_alert(get_provider("o/r"), "o/r", ["fabrik-alert"], "marker") is None


def test_alert_lookup_skips_non_dict_elements(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unerwartetes, nicht-Dict-Listenelement (Shape-Change) wird vom Provider
    übersprungen (kein AttributeError); gültige Issues dahinter werden geprüft."""
    marker = notify._ALERT_MARKER.format(fp=notify._alert_fingerprint(_failed("run-a")))
    issues = [
        "kaputtes Element",  # nicht-Dict → überspringen
        {"number": 2, "state": "open", "body": f"Issue-Body mit {marker}"},
    ]

    def _gh(method: str, url: str, token: str, payload: dict | None = None, accept=None) -> object:
        return issues

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _gh)
    assert notify._find_open_alert(get_provider("o/r"), "o/r", ["fabrik-alert"], marker) == 2


def test_alert_comment_post_failure_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kommentar-POST schlägt fehl → klarer Kanal-Eintrag, kein Crash."""
    fake = _FakeGitHub(open_issues=[{"number": 7, "state": "open", "body": "x"}])
    marker = notify._ALERT_MARKER.format(fp=notify._alert_fingerprint(_failed("run-a")))
    fake.issues[0]["body"] = marker

    def _gh_api(method: str, url: str, token: str, payload: dict | None = None, accept=None) -> object:
        if method == "GET":
            return fake.issues
        raise RuntimeError("gh down")

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _gh_api)
    assert notify._notify(_gh_spec(), _failed("run-a")) == ["issue:7(+):error(gh down)"]


def test_alert_issue_create_post_failure_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue-Anlage-POST schlägt fehl → klarer Kanal-Eintrag, kein Crash."""

    def _gh_api(method: str, url: str, token: str, payload: dict | None = None, accept=None) -> object:
        if method == "GET":
            return []
        raise RuntimeError("gh down")

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _gh_api)
    assert notify._notify(_gh_spec(), _failed("run-a")) == ["issue:error(gh down)"]


# ---------------------------------------------------------------- _step_notify
def _ctx() -> dict:
    return {"project": "p", "loop": "l", "run_id": "r1"}


def test_step_notify_no_channels() -> None:
    res = notify._step_notify(_ctx(), {"message": "hi"}, lambda m: None)
    assert res["ok"] is True
    assert "log only" in res["summary"]


def test_step_notify_slack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "_http_post", lambda url, payload: (200, "ok"))
    res = notify._step_notify(_ctx(), {"slack": "https://x", "message": "hi"}, lambda m: None)
    assert res["ok"] is True
    assert "200" in res["summary"]


def test_step_notify_webhook_fail_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "_http_post", lambda url, payload: (500, "err"))
    res = notify._step_notify(_ctx(), {"webhooks": [{"name": "w", "url": "https://x"}]}, lambda m: None)
    assert res["ok"] is False
    assert "500" in res["summary"]


def test_step_notify_slack_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(notify, "_http_post", lambda url, payload: seen.update(url=url) or (204, ""))
    res = notify._step_notify(_ctx(), {"slack": {"url": "https://dict"}}, lambda m: None)
    assert seen["url"] == "https://dict"
    assert res["ok"] is True


# ---------------------------------------------------------------- Retry-Ketten (ADR 0036)
def _retry_spec(events: list[str]) -> dict:
    return {"notify": {"events": events, "webhooks": [{"name": "dd", "url": "https://dd/x"}]}}


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    payloads: list[dict] = []
    monkeypatch.setattr(notify, "_http_post", lambda url, payload: payloads.append(payload) or (200, "ok"))
    return payloads


@pytest.mark.parametrize("state", ["pending", "dismissed", "resolved"])
def test_notify_silent_while_chain_pending_or_dismissed(posted: list[dict], state: str) -> None:
    assert notify._notify(_retry_spec(["run_failed", "run_exhausted"]), _record(), retry_state=state) == []
    assert posted == []


def test_notify_exhausted_reaches_run_failed_subscribers(posted: list[dict]) -> None:
    """Wer nur run_failed abonniert hat, bekommt auch den endgültigen Fehlschlag."""
    sent = notify._notify(_retry_spec(["run_failed"]), _record(), retry_state="exhausted")
    assert sent == ["dd:200"]
    assert posted[0]["event"] == "run_exhausted"


def test_notify_exhausted_only_subscription(posted: list[dict]) -> None:
    assert notify._notify(_retry_spec(["run_exhausted"]), _record(), retry_state="exhausted") == ["dd:200"]
    assert notify._notify(_retry_spec(["run_exhausted"]), _record()) == []  # run_failed nicht abonniert
