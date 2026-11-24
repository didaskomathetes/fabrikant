"""Step: git (fetch/branch/commit/push, geschützte Branches) + pr (GitHub-PR)."""

from __future__ import annotations

import subprocess

import pytest
from helpers import http_error, not_none

from fabrikant import gitops, validation
from fabrikant.engine import run_loop
from fabrikant.steps import git, pr


def _make_repo(tmp_path):
    """Lokales Git-Repo mit origin (bare) + einem Commit auf main."""
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


def _ctx(tmp_path):
    return {"project": "p", "loop": "l", "run_id": "r1", "workdir": str(tmp_path)}


# ---------------------------------------------------------------- _git
def test_git_raises_on_error(tmp_path):
    with pytest.raises(RuntimeError, match="git"):
        git._git(_ctx(tmp_path), tmp_path, "rev-parse", "--verify", "kein/ref")


def test_git_check_false_returns_stderr(tmp_path):
    # check=False: kein Raise, Output (hier stderr) wird zurückgegeben
    out = git._git(_ctx(tmp_path), tmp_path, "rev-parse", "--verify", "kein/ref", check=False)
    assert "not a git repository" in out


# ---------------------------------------------------------------- _step_git
def test_step_git_noop(tmp_path):
    res = git._step_git(_ctx(tmp_path), {})
    assert res["ok"] is True
    assert res["summary"] == "kein-op"


def test_step_git_branch_and_commit(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "new.txt").write_text("y")
    res = git._step_git(
        _ctx(repo),
        {"branch": "dev", "commit": {"message": "msg {run_id}"}},
    )
    assert res["ok"] is True
    assert "branch dev" in res["summary"]
    assert "commit" in res["summary"]
    cur = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert cur == "dev"
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%s"], capture_output=True, text=True
    ).stdout.strip()
    assert log == "msg r1"


def test_step_git_branch_substitutes_variables(tmp_path):
    # Regression (Issue #70): `branch: fabrikant/auto/{issue_number}` wurde wörtlich
    # als Branch-Name angelegt — Variablen müssen substituiert werden.
    repo = _make_repo(tmp_path)
    ctx = {**_ctx(repo), "issue_number": "70"}
    res = git._step_git(ctx, {"branch": "fabrikant/auto/{issue_number}"})
    assert res["ok"] is True
    assert "branch fabrikant/auto/70" in res["summary"]
    cur = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert cur == "fabrikant/auto/70"


def test_step_git_branch_names_missing_variable(tmp_path):
    # Manueller Run ohne --var issue_number: klare Meldung statt "unzulässige Zeichen".
    repo = _make_repo(tmp_path)
    with pytest.raises(ValueError, match=r"Variable\(n\) fehlen: issue_number \(manuell: --var issue_number=…\)"):
        git._step_git(_ctx(repo), {"branch": "fabrikant/relay/{issue_number}"})


def _subdir_repo(tmp_path):
    """Monorepo-Fall: workdir ist ein Subpfad, Änderung liegt an der Repo-Wurzel."""
    repo = _make_repo(tmp_path)
    sub = repo / "proj"
    sub.mkdir()
    (sub / "a.txt").write_text("a")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: push")
    return repo, sub


def _committed_files(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--format=", "HEAD"], capture_output=True, text=True
    ).stdout.split()


def test_step_git_commit_default_stages_repo_root_from_subdir(tmp_path):
    # Regression (Issue #83 / PR #201): `.github/` außerhalb des Subpfads fehlte im Commit
    repo, sub = _subdir_repo(tmp_path)
    res = git._step_git(_ctx(sub), {"commit": {"message": "m"}})
    assert res["ok"] is True
    assert sorted(_committed_files(repo)) == [".github/workflows/ci.yml", "proj/a.txt"]


def test_step_git_commit_blocks_on_unstaged_leftovers(tmp_path):
    # Enger Pathspec lässt Änderungen liegen → fail statt stillem Verwerfen
    repo, sub = _subdir_repo(tmp_path)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    res = git._step_git(_ctx(sub), {"commit": {"add": ["."], "message": "m"}, "push": True})
    assert res["ok"] is False
    assert ".github/workflows/ci.yml" in res["summary"]
    after = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    assert after == head  # kein Commit, kein Push


def test_step_git_commit_clean_tree_is_noop(tmp_path, monkeypatch):
    # Mock git-Aufruf, der 'nothing to commit' liefert → erfolgreicher No-Op
    repo = _make_repo(tmp_path)
    real_git = git._git

    def _fake_git(ctx, base, *args, check=True):
        if args[:2] == ("commit", "-m"):
            raise RuntimeError(f"git {' '.join(args)}: On branch main\nnothing to commit, working tree clean")
        return real_git(ctx, base, *args, check=check)

    monkeypatch.setattr(git, "_git", _fake_git)
    res = git._step_git(_ctx(repo), {"commit": {"message": "msg"}})
    assert res["ok"] is True
    assert "no-op" in res["summary"]


def test_step_git_commit_clean_tree_noop_real_repo(tmp_path):
    # Echte Integration: Repo ohne Änderungen → No-Op, kein Raise
    repo = _make_repo(tmp_path)
    res = git._step_git(_ctx(repo), {"commit": {"message": "msg"}})
    assert res["ok"] is True
    assert "no-op" in res["summary"]


def test_step_git_commit_real_error_still_raises(tmp_path, monkeypatch):
    # Andere Commit-Fehler bleiben fatal (Verhalten unverändert)
    repo = _make_repo(tmp_path)
    (repo / "new.txt").write_text("x")  # gestagte Änderung → Commit-Pfad wird erreicht
    real_git = git._git

    def _fake_git(ctx, base, *args, check=True):
        if args[:2] == ("commit", "-m"):
            raise RuntimeError("git commit -m msg: some other failure")
        return real_git(ctx, base, *args, check=check)

    monkeypatch.setattr(git, "_git", _fake_git)
    with pytest.raises(RuntimeError, match="some other failure"):
        git._step_git(_ctx(repo), {"commit": {"message": "msg"}})


def test_step_git_push(tmp_path):
    repo = _make_repo(tmp_path)
    res = git._step_git(_ctx(repo), {"branch": "dev", "push": True})
    assert res["ok"] is True
    assert "push dev" in res["summary"]


def test_step_git_push_protected(tmp_path):
    repo = _make_repo(tmp_path)
    res = git._step_git(_ctx(repo), {"push": True})  # HEAD = main (geschützt)
    assert res["ok"] is False
    assert "BLOCKED" in res["summary"]


def test_step_git_push_custom_protected(tmp_path):
    repo = _make_repo(tmp_path)
    res = git._step_git(_ctx(repo), {"branch": "dev", "push": True, "protected": ["dev"]})
    assert res["ok"] is False
    assert "dev" in res["summary"]


def test_run_loop_continues_after_clean_tree_commit(runs_dir, projects_dir, tmp_path):
    # run_loop bricht bei einem No-Op-Commit nicht ab und fährt mit dem
    # nächsten Step fort (Regression: Issue #62).
    root = tmp_path / "root"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "c1"], check=True)

    (projects_dir / "gitnoop.fabrikant.yaml").write_text(
        f"name: gitnoop\n"
        f"root: {root}\n"
        "loops:\n"
        "  s:\n"
        "    steps:\n"
        "      - name: commit\n"
        "        type: git\n"
        '        commit: {message: "msg"}\n'
        "      - name: after\n"
        "        type: shell\n"
        '        cmd: "echo done"\n'
    )
    rec = run_loop("gitnoop", "s", trigger="test")
    assert rec["status"] == "pass"
    by_name = {s["name"]: s for s in rec["steps"]}
    assert by_name["commit"]["status"] == "pass"
    assert "no-op" in by_name["commit"]["summary"]
    assert by_name["after"]["status"] == "pass"


# ---------------------------------------------------------------- _step_pr
def test_pr_missing_repo():
    with pytest.raises(ValueError, match="repo"):
        pr._step_pr(_ctx("/tmp"), {})


def test_pr_creates(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    seen = {}

    def _fake(method, url, token, payload=None):
        seen.update(method=method, url=url, token=token, payload=payload)
        return {"number": 7, "html_url": "https://gh/pr/7"}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _fake)
    ctx = _ctx(repo)
    res = pr._step_pr(ctx, {"repo": "o/r", "head": "dev"})
    assert res["ok"] is True
    assert "PR #7" in res["summary"]
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.github.com/repos/o/r/pulls"
    assert seen["payload"]["head"] == "dev"
    assert seen["payload"]["base"] == "main"
    assert "r1" in seen["payload"]["title"]  # {run_id} substituiert
    assert "labels" not in seen["payload"]  # labels nie im Create-Payload (GitHub ignoriert sie)
    # Folge-Steps (pr_review, pr_merge, human_gate) brauchen {pr_number}/{pr_url}
    assert ctx["pr_number"] == "7"
    assert ctx["pr_url"] == "https://gh/pr/7"


def test_pr_head_and_base_substitute_variables(monkeypatch, tmp_path):
    # Regression (Issue #70): `head: fabrikant/auto/{issue_number}` muss substituiert
    # werden, sonst zeigt die PR auf einen nicht existierenden Branch.
    repo = _make_repo(tmp_path)
    seen = {}

    def _fake(method, url, token, payload=None):
        seen.update(payload=payload)
        return {"number": 8, "html_url": "https://gh/pr/8"}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _fake)
    ctx = {**_ctx(repo), "issue_number": "70"}
    res = pr._step_pr(ctx, {"repo": "o/r", "head": "fabrikant/auto/{issue_number}", "base": "main"})
    assert res["ok"] is True
    assert seen["payload"]["head"] == "fabrikant/auto/70"
    assert seen["payload"]["base"] == "main"


def test_pr_reuses_existing_open_pr(monkeypatch, tmp_path):
    """Idempotenz: offener PR mit gleichem Head-Branch → wiederverwendet, kein Create."""
    repo = _make_repo(tmp_path)
    calls: list[tuple[str, str]] = []

    def _fake(method, url, token, payload=None):
        calls.append((method, url))
        if method == "GET":
            return [{"number": 42, "html_url": "https://gh/pr/42"}]
        return {"number": 43, "html_url": "https://gh/pr/43"}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _fake)
    ctx = _ctx(repo)
    res = pr._step_pr(ctx, {"repo": "o/r", "head": "dev"})
    assert res["ok"] is True
    assert "PR #42" in res["summary"]
    assert "wiederverwendet" in res["summary"]
    # Nur der Lookup-Call (head=owner:branch), kein Create-Call
    assert [m for m, _ in calls] == ["GET"]
    assert "head=o%3Adev" in calls[0][1]
    assert "state=open" in calls[0][1]
    # Folge-Steps (pr_review, pr_merge) bekommen die BESTEHENDE PR
    assert ctx["pr_number"] == "42"
    assert ctx["pr_url"] == "https://gh/pr/42"
    assert "pr_title" not in ctx  # API-Antwort ohne Titel → kein leerer Wert


@pytest.mark.parametrize("existing", [True, False])
def test_pr_exports_title_for_approval_context(monkeypatch, tmp_path, existing):
    """Issue #354: Approval-Gates nach dem pr-Step zeigen den PR-Titel (ADR 0011)."""
    repo = _make_repo(tmp_path)

    def _fake(method, url, token, payload=None):
        if method == "GET":
            return [{"number": 42, "html_url": "u", "title": "Alt-Titel"}] if existing else []
        return {"number": 43, "html_url": "u", "title": payload["title"]}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _fake)
    ctx = _ctx(repo)
    pr._step_pr(ctx, {"repo": "o/r", "head": "dev", "title": "Neu {run_id}"})
    assert ctx["pr_title"] == ("Alt-Titel" if existing else "Neu r1")


def test_pr_creates_when_lookup_empty(monkeypatch, tmp_path):
    """Kein offener PR für den Head-Branch → normaler Create-Pfad."""
    repo = _make_repo(tmp_path)
    calls: list[str] = []

    def _fake(method, url, token, payload=None):
        calls.append(method)
        if method == "GET":
            return []
        return {"number": 7, "html_url": "https://gh/pr/7"}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _fake)
    res = pr._step_pr(_ctx(repo), {"repo": "o/r", "head": "dev"})
    assert res["ok"] is True
    assert "PR #7" in res["summary"]
    assert calls == ["GET", "GET", "POST"]  # Lookup, Compare, Create


def _compare_fake(monkeypatch, compare: object) -> list[tuple[str, str]]:
    """Fake-GitHub: kein offener PR, Compare liefert `compare`, Create → PR #9."""
    calls: list[tuple[str, str]] = []

    def _fake(method, url, token, payload=None):
        calls.append((method, url))
        if "/compare/" in url:
            return compare
        if method == "GET":
            return []
        return {"number": 9, "html_url": "https://gh/pr/9"}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _fake)
    return calls


def test_pr_without_commits_ends_run_instead_of_create(monkeypatch, tmp_path):
    """Head ohne Commits vor base (Agent hat nichts geändert) → kein Create (GitHub: 422), Run endet."""
    calls = _compare_fake(monkeypatch, {"ahead_by": 0, "behind_by": 3})
    ctx = {**_ctx(_make_repo(tmp_path)), "issue_number": "181"}
    res = pr._step_pr(ctx, {"repo": "o/r", "base": "main", "head": "fabrikant/auto/{issue_number}"})
    assert res == {
        "ok": True,
        "done": True,
        "summary": "keine Commits auf fabrikant/auto/181 gegenüber main — kein PR",
    }
    # Branch-Namen sind im Compare-Pfad vollständig percent-encoded (`safe=""`).
    assert ("GET", "https://api.github.com/repos/o/r/compare/main...fabrikant%2Fauto%2F181") in calls
    assert all(m == "GET" for m, _ in calls)
    assert "pr_number" not in ctx


@pytest.mark.parametrize("compare", [{"ahead_by": 2}, {}, [], {"ahead_by": "0"}])
def test_pr_creates_when_commits_ahead_or_unknown(monkeypatch, tmp_path, compare):
    """Commits vor base oder unklare Compare-Antwort → normaler Create-Pfad."""
    calls = _compare_fake(monkeypatch, compare)
    res = pr._step_pr(_ctx(_make_repo(tmp_path)), {"repo": "o/r", "head": "dev"})
    assert "done" not in res
    assert "PR #9" in res["summary"]
    assert calls[-1] == ("POST", "https://api.github.com/repos/o/r/pulls")


def test_pr_compare_url_encodes_slashed_branch_names(monkeypatch, tmp_path):
    """`/` im Branch-Namen muss im Compare-Pfad percent-encoded sein (sonst 404)."""
    calls = _compare_fake(monkeypatch, {"ahead_by": 0})
    res = pr._step_pr(_ctx(_make_repo(tmp_path)), {"repo": "o/r", "base": "release/1.0", "head": "feature/foo"})
    assert res["done"] is True
    assert ("GET", "https://api.github.com/repos/o/r/compare/release%2F1.0...feature%2Ffoo") in calls


def test_pr_compare_http_error_falls_back_to_create(monkeypatch, tmp_path):
    """Compare-Fehler (z. B. 404: Head-Branch nicht auf dem Remote) → Create-Pfad,
    der die klarere GitHub-Fehlermeldung liefert."""
    calls: list[tuple[str, str]] = []

    def _fake(method, url, token, payload=None):
        calls.append((method, url))
        if "/compare/" in url:
            raise http_error(404, url, "Not Found")
        if method == "GET":
            return []
        return {"number": 9, "html_url": "https://gh/pr/9"}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _fake)
    res = pr._step_pr(_ctx(_make_repo(tmp_path)), {"repo": "o/r", "head": "dev"})
    assert "done" not in res
    assert "PR #9" in res["summary"]
    assert calls[-1] == ("POST", "https://api.github.com/repos/o/r/pulls")


def _label_calls(monkeypatch, *, existing: dict | None = None) -> list[tuple[str, str, dict | None]]:
    """Fake-GitHub für Label-Tests: protokolliert (method, url, payload) aller Calls.

    GET (Lookup) → [existing] oder []; POST /pulls → PR #8; sonst {}."""
    calls: list[tuple[str, str, dict | None]] = []

    def _fake(method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            return [existing] if existing else []
        if url.endswith("/pulls"):
            return {"number": 8, "html_url": "https://gh/pr/8"}
        return {}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _fake)
    return calls


def _labels_posted(calls) -> list[str] | None:
    """Payload-Labels des Issues-Labels-Calls (None, wenn keiner erfolgte)."""
    for method, url, payload in calls:
        if method == "POST" and url.endswith("/labels"):
            return payload["labels"]
    return None


def test_pr_labels_set_via_issues_api(monkeypatch, tmp_path):
    """labels: [fabrikant] → nach dem Create über issues/{n}/labels (POST /pulls ignoriert labels)."""
    calls = _label_calls(monkeypatch)
    res = pr._step_pr(_ctx(_make_repo(tmp_path)), {"repo": "o/r", "head": "dev", "labels": ["fabrikant"]})
    assert res["ok"] is True
    create = next(p for m, u, p in calls if u.endswith("/pulls") and m == "POST")
    assert "labels" not in not_none(create)
    assert ("POST", "https://api.github.com/repos/o/r/issues/8/labels", {"labels": ["fabrikant"]}) in calls


def test_pr_labels_added_to_reused_pr(monkeypatch, tmp_path):
    """Wiederverwendeter PR bekommt die Labels ebenfalls (heilt PRs ohne Label)."""
    calls = _label_calls(monkeypatch, existing={"number": 42, "html_url": "https://gh/pr/42"})
    pr._step_pr(_ctx(_make_repo(tmp_path)), {"repo": "o/r", "head": "dev", "labels": ["fabrikant"]})
    assert [m for m, _, _ in calls] == ["GET", "POST"]
    assert calls[1][1].endswith("/issues/42/labels")


def test_pr_labels_coerced_to_strings(monkeypatch, tmp_path):
    calls = _label_calls(monkeypatch)
    pr._step_pr(_ctx(_make_repo(tmp_path)), {"repo": "o/r", "head": "dev", "labels": ["a", 42]})
    assert _labels_posted(calls) == ["a", "42"]


def test_pr_labels_filtered_deduped(monkeypatch, tmp_path):
    """Leere/Whitespace-Labels gefiltert, Duplikate entfernt, Reihenfolge bleibt."""
    calls = _label_calls(monkeypatch)
    labels = ["fabrikant", "fabrikant", "", "  ", "ui"]
    pr._step_pr(_ctx(_make_repo(tmp_path)), {"repo": "o/r", "head": "dev", "labels": labels})
    assert _labels_posted(calls) == ["fabrikant", "ui"]


def test_pr_labels_all_empty_skips_call(monkeypatch, tmp_path):
    """Nur leere Labels → kein Labels-Call."""
    calls = _label_calls(monkeypatch)
    pr._step_pr(_ctx(_make_repo(tmp_path)), {"repo": "o/r", "head": "dev", "labels": ["", "  "]})
    assert _labels_posted(calls) is None


def test_pr_labels_none_and_dict_coerced(monkeypatch, tmp_path):
    """None/Dict-Einträge werden per str() coerced (dokumentiertes Verhalten)."""
    calls = _label_calls(monkeypatch)
    pr._step_pr(_ctx(_make_repo(tmp_path)), {"repo": "o/r", "head": "dev", "labels": [None, {"a": 1}]})
    assert _labels_posted(calls) == ["None", "{'a': 1}"]


# ---------------------------------------------------------------- validate_branch_name
@pytest.mark.parametrize("name", ["main", "fabrikant/auto/80", "feat/a_b-c.d", "a" * validation.MAX_BRANCH_NAME_LEN])
def test_validate_branch_name_accepts_valid(name):
    assert validation.validate_branch_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a b",
        "a\nb",
        "a;rm -rf",
        "a$(x)",
        "a`x`",
        "a|b",
        "-delete",
        "../etc",
        "a/../b",
        "a//b",
        "/abs",
        "a/",
        "a/.hidden",
        "x.lock",
        "a.",
        "a" * (validation.MAX_BRANCH_NAME_LEN + 1),
        None,
    ],
)
def test_validate_branch_name_rejects_invalid(name):
    with pytest.raises(ValueError):
        validation.validate_branch_name(name)


def test_step_git_rejects_invalid_branch_before_git(tmp_path):
    repo = _make_repo(tmp_path)
    with pytest.raises(ValueError):
        git._step_git(_ctx(repo), {"branch": "bad branch"})


def test_step_git_push_protected_branch_blocked(tmp_path):
    repo = _make_repo(tmp_path)
    res = git._step_git(_ctx(repo), {"push": True})
    assert res["ok"] is False
    assert "BLOCKED" in res["summary"]


def test_step_git_commit_noop_independent_of_locale(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANGUAGE", "de")
    res = git._step_git(_ctx(repo), {"commit": {"message": "msg"}})
    assert res["ok"] is True
    assert "no-op" in res["summary"]


def test_step_pr_rejects_invalid_head(tmp_path, monkeypatch):
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "t")
    with pytest.raises(ValueError):
        pr._step_pr(_ctx(tmp_path), {"repo": "o/r", "head": "bad branch"})
