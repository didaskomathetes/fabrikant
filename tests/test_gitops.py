"""gitops: _gh_token_safe, _git_fetch, ensure_worktree, _gh_api, _http_post."""

from __future__ import annotations

import subprocess
import urllib.error
from collections.abc import Callable

import pytest
from helpers import http_error

from fabrikant import gitops


# ---------------------------------------------------------------- _gh_token_safe
def test_gh_token_safe_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
    assert gitops._gh_token_safe() == "env-tok"


def test_gh_token_safe_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    envf = tmp_path / ".env"
    envf.write_text('GITHUB_TOKEN="file-tok"\n')
    monkeypatch.setenv("FABRIKANT_ENV_FILE", str(envf))
    assert gitops._gh_token_safe() == "file-tok"


def test_gh_token_safe_none(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("FABRIKANT_ENV_FILE", "/nonexistent/.env")
    assert gitops._gh_token_safe() == ""


def test_gh_token_raises_when_missing(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("FABRIKANT_ENV_FILE", "/nonexistent/.env")
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        gitops._gh_token()


# ---------------------------------------------------------------- _git_fetch
def _make_repo(tmp_path):
    """Lokales Git-Repo mit origin (bare) + einem Commit."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "c1"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "HEAD:refs/heads/main"], check=True)
    return repo


def test_git_fetch_ok(tmp_path):
    repo = _make_repo(tmp_path)
    gitops._git_fetch(repo)  # darf keine Exception werfen


def test_git_fetch_failure_no_token(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("FABRIKANT_ENV_FILE", "/nonexistent/.env")
    # origin auf ungültige URL → fetch schlägt fehl, kein Token → RuntimeError
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", "/nonexistent/origin.git"], check=True)
    with pytest.raises(RuntimeError, match="git fetch"):
        gitops._git_fetch(repo)


# ---------------------------------------------------------------- ensure_worktree
def test_ensure_worktree_no_repo(tmp_path, monkeypatch):
    from fabrikant import config

    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    spec = {"name": "p", "root": str(tmp_path)}
    assert gitops.ensure_worktree(spec, "r1") == tmp_path


def test_ensure_worktree_creates(tmp_path, monkeypatch):
    from fabrikant import config

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    spec = {"name": "p", "root": str(repo), "repo": {"branch": "main"}}
    base = gitops.ensure_worktree(spec, "r1")
    assert base.is_dir()
    assert (base / "f.txt").is_file()


def test_ensure_worktree_fetches_target_branch(tmp_path, monkeypatch):
    from fabrikant import config

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    fetched = []
    original_fetch = gitops._git_fetch

    def record_fetch(repo_root, ref=None):
        fetched.append((repo_root, ref))
        original_fetch(repo_root, ref)

    monkeypatch.setattr(gitops, "_git_fetch", record_fetch)
    gitops.ensure_worktree(_spec(repo), "r1")

    assert fetched == [(repo, "main")]


def test_ensure_worktree_subpath_missing(tmp_path, monkeypatch):
    from fabrikant import config

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    spec = {"name": "p", "root": str(repo), "repo": {"branch": "main", "subpath": "does/not/exist"}}
    with pytest.raises(RuntimeError, match="Subpfad"):
        gitops.ensure_worktree(spec, "r1")


def _spec(repo, name="p"):
    return {"name": name, "root": str(repo), "repo": {"branch": "main"}}


def test_ensure_worktree_separate_per_run(tmp_path, monkeypatch):
    """Jeder Run bekommt einen eigenen Worktree (ADR 0028)."""
    from fabrikant import config

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    a = gitops.ensure_worktree(_spec(repo), "r1")
    b = gitops.ensure_worktree(_spec(repo), "r2")
    assert a != b
    assert a == tmp_path / "wt" / "p" / "r1"
    (a / "only-a.txt").write_text("x")
    assert not (b / "only-a.txt").exists()


def test_ensure_worktree_resume_keeps_existing_state(tmp_path, monkeypatch):
    """Resume: existierender Run-Worktree bleibt unangetastet (kein fetch/reset)."""
    from fabrikant import config

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    base = gitops.ensure_worktree(_spec(repo), "r1")
    subprocess.run(["git", "-C", str(base), "checkout", "-qB", "feature/work"], check=True)
    (base / "untracked.txt").write_text("state")
    again = gitops.ensure_worktree(_spec(repo), "r1")
    assert again == base
    head = subprocess.run(
        ["git", "-C", str(base), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert head == "feature/work"
    assert (base / "untracked.txt").is_file()


def test_remove_worktree_and_cleanup_run_worktree(tmp_path, monkeypatch, projects_dir):
    from fabrikant import config

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    (projects_dir / "p.fabrikant.yaml").write_text(
        f"name: p\nroot: {repo}\nrepo: {{branch: main}}\nloops:\n  l:\n    steps:\n      - {{type: report, text: x}}\n"
    )
    base = gitops.ensure_worktree(_spec(repo), "r1")
    assert gitops.cleanup_run_worktree("p", "r1") is True
    assert not base.exists()
    assert gitops.cleanup_run_worktree("p", "r1") is True  # idempotent
    assert gitops.cleanup_run_worktree("unbekannt", "r1") is True  # unbekanntes Projekt: No-Op
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert "r1" not in listed


def test_remove_worktree_fallback_rmtree_reports_git_failure(tmp_path):
    """Kein gültiger Worktree: Verzeichnis weg, aber Git-Fehler bleibt sichtbar."""
    repo = _make_repo(tmp_path)
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "f").write_text("x")
    with pytest.raises(subprocess.CalledProcessError):
        gitops.remove_worktree(repo, stray)
    assert not stray.exists()
    assert gitops.remove_worktree(repo, stray) is True  # fehlender Pfad: No-Op


def test_remove_worktree_reports_prune_failure(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    stray = tmp_path / "stray"
    stray.mkdir()
    real_run = gitops.subprocess.run

    def fail_prune(args, **kwargs):
        if args[-2:] == ["worktree", "prune"]:
            return subprocess.CompletedProcess(args, 1, "", "prune failed")
        return real_run(args, **kwargs)

    monkeypatch.setattr(gitops.subprocess, "run", fail_prune)
    with pytest.raises(subprocess.CalledProcessError, match="prune"):
        gitops.remove_worktree(repo, stray)


def test_remove_worktree_reports_remaining_dir(tmp_path, monkeypatch):
    """Verzeichnis lässt sich nicht entfernen → Git-Fehler bleibt sichtbar."""
    repo = _make_repo(tmp_path)
    stray = tmp_path / "stray"
    stray.mkdir()
    monkeypatch.setattr(gitops.shutil, "rmtree", lambda *a, **k: None)
    with pytest.raises(subprocess.CalledProcessError):
        gitops.remove_worktree(repo, stray)
    assert stray.exists()


def test_cleanup_orphaned_worktrees(tmp_path, monkeypatch, projects_dir, runs_dir):
    """Beim Start: verwaiste Run-Worktrees und Legacy-Worktrees weg; needs_approval bleibt."""
    import json

    from fabrikant import config

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    (projects_dir / "p.fabrikant.yaml").write_text(
        f"name: p\nroot: {repo}\nrepo: {{branch: main}}\nloops:\n  l:\n    steps:\n      - {{type: report, text: x}}\n"
    )
    crashed = gitops.ensure_worktree(_spec(repo), "crashed")
    paused = gitops.ensure_worktree(_spec(repo), "paused")
    active = gitops.ensure_worktree(_spec(repo), "active")
    (runs_dir / "p").mkdir()
    (runs_dir / "p" / "crashed.json").write_text(json.dumps({"id": "crashed", "status": "running"}))
    (runs_dir / "p" / "paused.json").write_text(json.dumps({"id": "paused", "status": "needs_approval"}))
    removed = gitops.cleanup_orphaned_worktrees(active_run_ids={"active"})
    assert [p.name for p in removed] == ["crashed"]
    assert not crashed.exists()
    assert paused.exists()
    assert active.exists()


def test_cleanup_orphaned_worktrees_legacy_layout(tmp_path, monkeypatch, projects_dir):
    from fabrikant import config

    repo = _make_repo(tmp_path)
    wt_root = tmp_path / "wt"
    monkeypatch.setattr(config, "WORKTREES_DIR", wt_root)
    (projects_dir / "p.fabrikant.yaml").write_text(
        f"name: p\nroot: {repo}\nrepo: {{branch: main}}\nloops:\n  l:\n    steps:\n      - {{type: report, text: x}}\n"
    )
    legacy = wt_root / "p"
    legacy.parent.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(legacy), "HEAD"], check=True)
    assert gitops.cleanup_orphaned_worktrees() == [legacy]
    assert not legacy.exists()


def test_cleanup_orphaned_worktrees_empty(tmp_path, monkeypatch):
    from fabrikant import config

    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "nichts")
    assert gitops.cleanup_orphaned_worktrees() == []


def test_worktree_sync_check(tmp_path):
    repo = _make_repo(tmp_path)
    info = gitops._worktree_sync_check({"root": str(repo)}, repo)
    assert info["branch"] == "main"
    # head ist die Commit-SHA (40 Hex), nicht das Branch-Name (Bug-Fix)
    assert len(info["head"]) == 40
    assert all(c in "0123456789abcdef" for c in info["head"])


# ---------------------------------------------------------------- _gh_api / _http_post
class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_gh_api(monkeypatch):
    monkeypatch.setattr(gitops.urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(200, '{"a": 1}'))
    res = gitops._gh_api("GET", "https://api.github.com/x", "tok")
    assert res == {"a": 1}


def test_gh_api_empty_body(monkeypatch):
    monkeypatch.setattr(gitops.urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(200, ""))
    res = gitops._gh_api("GET", "https://api.github.com/x", "tok")
    assert res == {}


def test_gh_api_retries_transient_network_errors(monkeypatch):
    """DNS-Flakes/Resets → Retry, danach Erfolg."""
    monkeypatch.setattr(gitops.config, "GH_RETRY_DELAY_S", 0)
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("No address associated with hostname")
        return _FakeResp(200, '{"a": 1}')

    monkeypatch.setattr(gitops.urllib.request, "urlopen", _fake)
    res = gitops._gh_api("GET", "https://api.github.com/x", "tok")
    assert res == {"a": 1}
    assert calls["n"] == 3


def test_gh_api_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(gitops.config, "GH_RETRY_DELAY_S", 0)
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("DNS down")

    monkeypatch.setattr(gitops.urllib.request, "urlopen", _fake)
    with pytest.raises(urllib.error.URLError):
        gitops._gh_api("GET", "https://api.github.com/x", "tok")
    assert calls["n"] == gitops.config.GH_RETRIES


def test_gh_api_http_error_not_retried(monkeypatch):
    """4xx/5xx ist eine Antwort, kein Flake — kein Retry."""
    monkeypatch.setattr(gitops.config, "GH_RETRY_DELAY_S", 0)
    calls = {"n": 0}
    err = http_error(401, "https://x", "unauthorized")

    def _fake(req, timeout=None):
        calls["n"] += 1
        raise err

    monkeypatch.setattr(gitops.urllib.request, "urlopen", _fake)
    with pytest.raises(urllib.error.HTTPError):
        gitops._gh_api("GET", "https://api.github.com/x", "tok")
    assert calls["n"] == 1


def test_gh_diff_retries_transient_network_errors(monkeypatch):
    monkeypatch.setattr(gitops.config, "GH_RETRY_DELAY_S", 0)
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError("reset")
        return _FakeResp(200, "diff --git a/x b/x")

    monkeypatch.setattr(gitops.urllib.request, "urlopen", _fake)
    assert gitops._gh_diff("o/r", 7, "tok") == "diff --git a/x b/x"
    assert calls["n"] == 2


def test_http_post_ok(monkeypatch):
    monkeypatch.setattr(gitops.urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(200, "ok"))
    code, body = gitops._http_post("https://x", {"a": 1})
    assert code == 200
    assert body == "ok"


def test_http_post_httperror(monkeypatch):
    err = http_error(500, body=b"boom")

    def _fake(req, timeout=None):
        raise err

    monkeypatch.setattr(gitops.urllib.request, "urlopen", _fake)
    code, body = gitops._http_post("https://x", {"a": 1})
    assert code == 500
    assert body == "boom"


def test_http_post_exception(monkeypatch):
    def _fake(req, timeout=None):
        raise RuntimeError("conn refused")

    monkeypatch.setattr(gitops.urllib.request, "urlopen", _fake)
    code, body = gitops._http_post("https://x", {"a": 1})
    assert code == 0
    assert "conn refused" in body


# ---------------------------------------------------------------- _gh_dependencies (ADR 0031)
@pytest.mark.parametrize("kind", ["blocked_by", "blocking"])
def test_gh_dependencies_url_per_kind(monkeypatch, kind):
    calls = []
    monkeypatch.setattr(gitops, "_gh_api", lambda m, u, t: calls.append((m, u)) or [{"number": 1}])
    assert gitops._gh_dependencies("o/r", 7, "tok", kind) == [{"number": 1}]
    assert calls == [("GET", f"https://api.github.com/repos/o/r/issues/7/dependencies/{kind}?per_page=100")]


def test_gh_dependencies_empty_and_invalid_kind(monkeypatch):
    monkeypatch.setattr(gitops, "_gh_api", lambda m, u, t: {})
    assert gitops._gh_dependencies("o/r", 7, "tok") == []
    with pytest.raises(ValueError, match="Dependency-Art"):
        gitops._gh_dependencies("o/r", 7, "tok", "parent")


# ---------------------------------------------------------------- Legacy-Worktree-Wurzel (ADR 0047, Nachtrag)
def test_run_worktree_path_new_root_by_default(tmp_path, monkeypatch):
    from fabrikant import config

    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    monkeypatch.setattr(config, "LEGACY_WORKTREES_DIR", tmp_path / "legacy")
    assert gitops.run_worktree_path("p", "r1") == tmp_path / "wt" / "p" / "r1"


def test_run_worktree_path_prefers_existing_legacy(tmp_path, monkeypatch):
    """Vor dem Umzug pausierter Run (needs_approval) setzt im alten Worktree fort."""
    from fabrikant import config

    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    monkeypatch.setattr(config, "LEGACY_WORKTREES_DIR", tmp_path / "legacy")
    legacy_wt = tmp_path / "legacy" / "p" / "r1"
    legacy_wt.mkdir(parents=True)
    (legacy_wt / ".git").write_text("gitdir: /repo/.git/worktrees/r1\n")
    assert gitops.run_worktree_path("p", "r1") == legacy_wt
    assert gitops.run_worktree_path("p", "r2") == tmp_path / "wt" / "p" / "r2"


def test_run_worktree_path_ignores_empty_legacy_dir(tmp_path, monkeypatch):
    """Ein leerer/verwaister Legacy-Ordner (kein echter Worktree) verdeckt die neue Wurzel nicht."""
    from fabrikant import config

    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    monkeypatch.setattr(config, "LEGACY_WORKTREES_DIR", tmp_path / "legacy")
    (tmp_path / "legacy" / "p" / "r1").mkdir(parents=True)  # kein `.git` → kein Worktree
    assert gitops.run_worktree_path("p", "r1") == tmp_path / "wt" / "p" / "r1"


def test_worktree_roots_dedups_same_dir(tmp_path, monkeypatch):
    from fabrikant import config

    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    monkeypatch.setattr(config, "LEGACY_WORKTREES_DIR", tmp_path / "wt")
    assert gitops.worktree_roots() == [tmp_path / "wt"]


def test_cleanup_orphaned_worktrees_covers_legacy_root(tmp_path, monkeypatch, projects_dir, runs_dir):
    import json

    from fabrikant import config

    repo = _make_repo(tmp_path)
    legacy_root = tmp_path / "legacy"
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    monkeypatch.setattr(config, "LEGACY_WORKTREES_DIR", legacy_root)
    (projects_dir / "p.fabrikant.yaml").write_text(
        f"name: p\nroot: {repo}\nrepo: {{branch: main}}\nloops:\n  l:\n    steps:\n      - {{type: report, text: x}}\n"
    )
    for rid in ("old-crashed", "old-paused"):
        wt = legacy_root / "p" / rid
        wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), "HEAD"], check=True)
    new_crashed = gitops.ensure_worktree(_spec(repo), "new-crashed")
    (runs_dir / "p").mkdir()
    (runs_dir / "p" / "old-paused.json").write_text(json.dumps({"id": "old-paused", "status": "needs_approval"}))

    removed = gitops.cleanup_orphaned_worktrees()

    assert sorted(p.name for p in removed) == ["new-crashed", "old-crashed"]
    assert not new_crashed.exists()
    assert (legacy_root / "p" / "old-paused").exists()


# ---------------------------------------------------------------- repo_lock (Issue #460)
# git-Befehle, die `.git/worktrees` bzw. die Ref-Walks über alle Worktree-HEADs berühren.
_REPO_MUTATING = ("fetch", "worktree")


def test_git_common_dir_shared_by_repo_and_worktree(tmp_path, monkeypatch):
    from fabrikant import config

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    wt = gitops.ensure_worktree(_spec(repo), "r1")
    common = gitops._git_common_dir(repo)
    assert common == (repo / ".git").resolve()
    assert gitops._git_common_dir(wt) == common  # ein Lock für Repo + alle Worktrees


def test_repo_lock_outside_git_is_noop(tmp_path):
    assert gitops._git_common_dir(tmp_path) is None
    with gitops.repo_lock(tmp_path):
        pass
    assert not list(tmp_path.iterdir())  # kein Lockfile angelegt


def test_parallel_worktree_ops_are_serialized(tmp_path, monkeypatch):
    """Regression #460: fetch und worktree add/remove desselben Repos dürfen nie überlappen
    (paralleles fetch sah ein halb angelegtes `.git/worktrees/<id>/HEAD` → `bad object`)."""
    import threading
    import time

    from fabrikant import config

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(config, "WORKTREES_DIR", tmp_path / "wt")
    doomed = gitops.ensure_worktree(_spec(repo), "doomed")
    state = {"active": 0, "max": 0}
    guard = threading.Lock()
    real_run = subprocess.run

    def tracking_run(argv, *args, **kwargs):
        mutating = any(cmd in argv for cmd in _REPO_MUTATING)
        if mutating:
            with guard:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.05)  # Überlappungsfenster vergrößern
        try:
            return real_run(argv, *args, **kwargs)
        finally:
            if mutating:
                with guard:
                    state["active"] -= 1

    monkeypatch.setattr(gitops.subprocess, "run", tracking_run)
    errors: list[Exception] = []

    def attempt(fn):
        try:
            fn()
        except Exception as e:  # pragma: no cover - nur bei Regression
            errors.append(e)

    jobs: list[Callable[[], object]] = [lambda i=i: gitops.ensure_worktree(_spec(repo), f"r{i}") for i in range(4)]
    jobs.append(lambda: gitops.remove_worktree(repo, doomed))
    threads = [threading.Thread(target=attempt, args=(job,)) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert state["max"] == 1
    assert not doomed.exists()
    assert all((tmp_path / "wt" / "p" / f"r{i}").is_dir() for i in range(4))
