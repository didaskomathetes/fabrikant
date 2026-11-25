"""netretry: git network commands survive short DNS/connection blips."""

from __future__ import annotations

import socket
import subprocess
import urllib.error

import pytest

from fabrikant import config, netretry

DNS_DOWN = "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com"


@pytest.fixture
def fake_git(monkeypatch):
    """Replace subprocess.run with a sequence of (returncode, stderr); record the waits."""
    monkeypatch.setattr(config, "NET_RETRY_DELAYS_S", (5.0, 15.0))
    results: list[tuple[int, str]] = []
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        code, err = results.pop(0)
        stderr = err if kwargs.get("text") else err.encode()
        return subprocess.CompletedProcess(cmd, code, "" if kwargs.get("text") else b"", stderr)

    monkeypatch.setattr(netretry.subprocess, "run", fake_run)
    slept: list[float] = []
    return results, calls, slept


CMD = ["git", "-C", "/repo", "fetch", "origin"]


def test_run_git_retries_transient_until_success(fake_git):
    results, calls, slept = fake_git
    results += [(128, DNS_DOWN), (0, "")]
    p = netretry.run_git(CMD, timeout=1, sleep=slept.append)
    assert p.returncode == 0 and len(calls) == 2 and slept == [5.0]


def test_run_git_gives_up_after_all_delays(fake_git):
    results, calls, slept = fake_git
    results += [(128, DNS_DOWN)] * 3
    p = netretry.run_git(CMD, timeout=1, sleep=slept.append)
    assert p.returncode == 128 and len(calls) == 3 and slept == [5.0, 15.0]


def test_run_git_does_not_retry_non_transient(fake_git):
    results, calls, slept = fake_git
    results += [(128, "fatal: Authentication failed for 'https://github.com/o/r.git/'")]
    assert netretry.run_git(CMD, timeout=1, sleep=slept.append).returncode == 128
    assert len(calls) == 1 and not slept


def test_run_git_bytes_mode_and_log_hides_url(fake_git, caplog):
    results, _, slept = fake_git
    results += [(128, DNS_DOWN), (0, "")]
    cmd = ["git", "-C", "/repo", "fetch", "https://x-access-token:SECRET@github.com/o/r.git"]
    assert netretry.run_git(cmd, timeout=1, text=False, sleep=slept.append).returncode == 0
    assert "fetch" in caplog.text and "SECRET" not in caplog.text


def test_delays_override():
    assert netretry.delays((1, 2)) == (1, 2)
    assert netretry.delays() == tuple(config.NET_RETRY_DELAYS_S)


# ── Callers: network subcommands go through run_git, local ones do not ──


def _record_run_git(monkeypatch, module) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake(cmd, **kwargs):
        seen.append(cmd)
        out = "" if kwargs.get("text", True) else b""
        return subprocess.CompletedProcess(cmd, 0, out, out)

    monkeypatch.setattr(module.netretry, "run_git", fake)
    return seen


def test_git_step_retries_only_network_subcommands(monkeypatch, tmp_path):
    from fabrikant.steps import git as git_step

    seen = _record_run_git(monkeypatch, git_step)
    git_step._git({}, tmp_path, "fetch", "origin")
    assert seen == [["git", "-C", str(tmp_path), "fetch", "origin"]]
    monkeypatch.setattr(git_step.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "x", ""))
    assert git_step._git({}, tmp_path, "status") == "x" and len(seen) == 1


def test_apicommit_push_goes_through_run_git(monkeypatch, tmp_path):
    from fabrikant import apicommit

    seen = _record_run_git(monkeypatch, apicommit)
    apicommit._run_git(tmp_path, "push", "origin", "b")
    assert seen == [["git", "-C", str(tmp_path), "push", "origin", "b"]]


def test_gitlab_request_retries_network_errors(monkeypatch):
    import urllib.error

    from fabrikant.hosting import gitlab

    monkeypatch.setenv(gitlab.TOKEN_ENV, "tok")
    monkeypatch.setattr(config, "NET_RETRY_DELAYS_S", (0, 0))
    monkeypatch.setattr(gitlab.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("temporary failure in name resolution")
        return _Resp()

    monkeypatch.setattr(gitlab.httpsafe, "urlopen", flaky)
    assert gitlab.GitLabProvider(host="gitlab.example.com")._request("GET", "/user") == {"ok": True}
    assert calls["n"] == 3

    calls["n"] = -10  # never recovers within the budget
    with pytest.raises(urllib.error.URLError):
        gitlab.GitLabProvider(host="gitlab.example.com")._request("GET", "/user")
    assert calls["n"] == -7


@pytest.mark.parametrize(
    ("method", "exc", "expected"),
    [
        ("GET", TimeoutError("read timed out"), True),
        ("PATCH", ConnectionResetError("reset"), True),
        ("put", urllib.error.URLError("x"), True),
        ("POST", urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution")), True),
        ("POST", urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), True),
        ("POST", urllib.error.URLError(TimeoutError("timed out")), False),
        ("POST", ConnectionResetError("reset"), False),
        ("POST", TimeoutError("read timed out"), False),
    ],
)
def test_may_replay_only_safe_methods_or_unsent_requests(method, exc, expected):
    assert netretry.may_replay(method, exc) is expected


def _count_urlopen(monkeypatch, module, exc: BaseException) -> dict:
    calls = {"n": 0}

    def fail(req, timeout=None):
        calls["n"] += 1
        raise exc

    monkeypatch.setattr(config, "NET_RETRY_DELAYS_S", (0, 0))
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    monkeypatch.setattr(module.httpsafe, "urlopen", fail)
    return calls


def _github_post():
    from fabrikant import gitops

    gitops._gh_api("POST", "https://api.github.com/repos/o/r/issues", "tok", {"title": "t"})


def _gitlab_post():
    from fabrikant.hosting import gitlab

    gitlab.GitLabProvider(host="gitlab.example.com")._request("POST", "/projects/1/issues", {"title": "t"})


@pytest.mark.parametrize("post", [_github_post, _gitlab_post])
def test_post_is_not_replayed_after_ambiguous_error(monkeypatch, post):
    """Regression: a reset while reading the answer can follow a committed write — replaying the POST
    would create the issue/comment twice."""
    from fabrikant import gitops
    from fabrikant.hosting import gitlab

    monkeypatch.setenv(gitlab.TOKEN_ENV, "tok")
    calls = _count_urlopen(monkeypatch, gitops if post is _github_post else gitlab, ConnectionResetError("reset"))
    with pytest.raises(ConnectionResetError):
        post()
    assert calls["n"] == 1


@pytest.mark.parametrize("post", [_github_post, _gitlab_post])
def test_post_is_retried_when_it_never_left(monkeypatch, post):
    """DNS down: the request never reached the server — a replay cannot duplicate anything."""
    from fabrikant import gitops
    from fabrikant.hosting import gitlab

    monkeypatch.setenv(gitlab.TOKEN_ENV, "tok")
    dns = urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution"))
    calls = _count_urlopen(monkeypatch, gitops if post is _github_post else gitlab, dns)
    with pytest.raises(urllib.error.URLError):
        post()
    assert calls["n"] == 3


@pytest.mark.parametrize(
    ("url", "payload", "expected"),
    [
        ("https://api.github.com/repos/o/r/git/blobs", {"content": "x"}, True),
        ("https://api.github.com/repos/o/r/git/trees", {"tree": []}, True),
        ("https://api.github.com/graphql", {"query": "  query($n: Int!) { a }"}, True),
        ("https://api.github.com/graphql", {"query": "{ viewer { login } }"}, True),
        ("https://api.github.com/graphql", {"query": "mutation { resolveReviewThread }"}, False),
        ("https://api.github.com/graphql", None, False),
        ("https://api.github.com/repos/o/r/git/refs", {"ref": "refs/heads/b"}, False),
        ("https://api.github.com/repos/o/r/issues", {"title": "t"}, False),
    ],
)
def test_side_effect_free_posts_are_replayed(monkeypatch, url, payload, expected):
    """Git objects are content-addressed and GraphQL queries only read: a replay after a timeout cannot
    duplicate anything, so they keep the full retry schedule (mutations and other writes do not)."""
    from fabrikant import gitops

    calls = _count_urlopen(monkeypatch, gitops, TimeoutError("read timed out"))
    with pytest.raises(TimeoutError):
        gitops._gh_api("POST", url, "tok", payload)
    assert calls["n"] == (3 if expected else 1)
