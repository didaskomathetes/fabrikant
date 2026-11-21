"""Unit tests for the hosting-provider abstraction (ADR 0062).

Covers the pure contract logic (repo parsing, payload normalization, provider
selection, GitLab token resolution, webhook parsing) and the documented
provider gaps — no network (adapter I/O is mocked where exercised).
"""

# pyright: reportArgumentType=false
from __future__ import annotations

import pytest

from fabrik import gitops
from fabrik.hosting import (
    BaseProvider,
    CompareResult,
    GitHubProvider,
    GitLabProvider,
    HostingConfigError,
    HostingError,
    HostingNotSupported,
    Issue,
    PullRequest,
    RepoRef,
    get_provider,
    spec_context,
)
from fabrik.hosting import gitlab as gitlab_mod
from fabrik.hosting import registry as registry_mod

# --------------------------------------------------------------------------- GitHub: parse_repo


class TestGitHubParseRepo:
    def test_slug(self):
        r = GitHubProvider().parse_repo("owner/name")
        assert r == RepoRef("github.com", "owner", "name")
        assert r.slug == "owner/name"

    def test_slug_with_git_suffix(self):
        assert GitHubProvider().parse_repo("owner/name.git").slug == "owner/name"

    def test_https_url(self):
        r = GitHubProvider().parse_repo("https://github.com/owner/name")
        assert (r.host, r.slug) == ("github.com", "owner/name")

    def test_ssh_url(self):
        r = GitHubProvider().parse_repo("git@github.com:owner/name")
        assert (r.host, r.slug) == ("github.com", "owner/name")

    def test_unparseable_raises(self):
        with pytest.raises(HostingConfigError):
            GitHubProvider().parse_repo("just-a-name")

    def test_url_without_name_raises(self):
        with pytest.raises(HostingConfigError):
            GitHubProvider().parse_repo("https://github.com/owner/")


# --------------------------------------------------------------------------- GitLab: parse_repo


class TestGitLabParseRepo:
    def test_slug_uses_default_host(self):
        r = GitLabProvider(host="gitlab.example.com").parse_repo("owner/name")
        assert (r.host, r.slug) == ("gitlab.example.com", "owner/name")

    def test_https_url_keeps_host(self):
        r = GitLabProvider().parse_repo("https://gitlab.opencode.de/group/project")
        assert (r.host, r.slug) == ("gitlab.opencode.de", "group/project")

    def test_https_url_strips_git_suffix(self):
        assert GitLabProvider().parse_repo("https://h.co/o/n.git").slug == "o/n"

    def test_ssh_url(self):
        r = GitLabProvider().parse_repo("git@gitlab.example.com:owner/name")
        assert (r.host, r.slug) == ("gitlab.example.com", "owner/name")

    def test_unparseable_slug_raises(self):
        with pytest.raises(HostingConfigError):
            GitLabProvider().parse_repo("no-slash")

    def test_url_without_name_raises(self):
        with pytest.raises(HostingConfigError):
            GitLabProvider().parse_repo("https://h.co/onlyowner")


# --------------------------------------------------------------------------- normalization


class TestGitHubNormalization:
    def test_issue_from(self):
        raw = {
            "number": 7,
            "title": "t",
            "body": "b",
            "state": "open",
            "labels": [{"name": "a"}, {"name": "b"}],
            "html_url": "https://u/7",
        }
        from fabrik.hosting.github import _issue_from

        out = _issue_from(raw)
        assert out.number == 7
        assert out.labels == ["a", "b"]
        assert out.is_pr is False

    def test_pr_from(self):
        from fabrik.hosting.github import _pr_from

        raw = {
            "number": 3,
            "title": "pr",
            "body": "x",
            "state": "open",
            "labels": [{"name": "fabrik"}],
            "html_url": "https://u/pr/3",
            "head": {"ref": "feat", "sha": "abc"},
            "base": {"ref": "main"},
            "mergeable": True,
        }
        out = _pr_from(raw)
        assert (out.head_ref, out.head_sha, out.base_ref) == ("feat", "abc", "main")
        assert out.mergeable is True
        assert out.labels == ["fabrik"]

    def test_pr_from_missing_mergeable(self):
        from fabrik.hosting.github import _pr_from

        out = _pr_from({"number": 1, "head": {}, "base": {}})
        assert out.mergeable is None
        assert out.head_sha == ""

    def test_file_from(self):
        from fabrik.hosting.github import _file_from

        out = _file_from({"filename": "a.py", "status": "modified", "additions": 2, "deletions": 1, "patch": "p"})
        assert (out.path, out.status, out.additions, out.deletions, out.patch) == ("a.py", "modified", 2, 1, "p")


class TestGitLabNormalization:
    def test_issue_from_labels_as_list(self):
        from fabrik.hosting.gitlab import _issue_from

        out = _issue_from({"iid": 5, "title": "t", "description": "d", "state": "opened", "labels": ["x", "y"]})
        assert out.number == 5
        assert out.labels == ["x", "y"]
        assert out.is_pr is False

    def test_issue_from_labels_as_comma_string(self):
        from fabrik.hosting.gitlab import _issue_from

        out = _issue_from({"iid": 5, "labels": "a, b,,c"})
        assert out.labels == ["a", "b", "c"]

    def test_pr_from(self):
        from fabrik.hosting.gitlab import _pr_from

        raw = {
            "iid": 9,
            "title": "mr",
            "description": "d",
            "state": "opened",
            "labels": ["l"],
            "web_url": "https://u/mr/9",
            "source_branch": "feat",
            "sha": "sha1",
            "target_branch": "main",
        }
        out = _pr_from(raw)
        assert (out.head_ref, out.head_sha, out.base_ref) == ("feat", "sha1", "main")
        assert out.mergeable is None  # GitLab does not report mergeable in the list/get payload

    def test_file_from_counts_and_status(self):
        from fabrik.hosting.gitlab import _file_from

        diff = "+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n+extra\n"
        out = _file_from({"new_path": "a.py", "diff": diff, "new_file": True})
        assert out.status == "added"
        assert out.additions == 2
        assert out.deletions == 1

    def test_file_from_deleted(self):
        from fabrik.hosting.gitlab import _file_from

        assert _file_from({"new_path": "a.py", "diff": "", "deleted_file": True}).status == "removed"


# --------------------------------------------------------------------------- selection


class TestGetProvider:
    def test_explicit_gitlab(self):
        assert isinstance(get_provider("o/n", spec={"hosting": "gitlab"}), GitLabProvider)

    def test_explicit_github(self):
        assert isinstance(get_provider("o/n", spec={"hosting": "github"}), GitHubProvider)

    def test_repo_url_github_host(self):
        assert isinstance(get_provider("https://github.com/o/n"), GitHubProvider)

    def test_repo_url_other_host_is_gitlab(self):
        p = get_provider("https://gitlab.opencode.de/o/n")
        assert isinstance(p, GitLabProvider)
        assert p.parse_repo("o/n").host == "gitlab.opencode.de"

    def test_github_field_defaults_to_github(self):
        assert isinstance(get_provider("o/n", spec={"github": "o/n"}), GitHubProvider)

    def test_default_is_github(self):
        assert isinstance(get_provider("o/n"), GitHubProvider)

    def test_gitlab_host_from_spec(self):
        p = get_provider("o/n", spec={"hosting": "gitlab", "gitlab_host": "gl.example.com"})
        assert p.parse_repo("o/n").host == "gl.example.com"

    def test_gitlab_host_env_override(self, monkeypatch):
        monkeypatch.setenv(gitlab_mod.GITLAB_HOST_ENV, "env-gl.example.com")
        p = get_provider("o/n", spec={"hosting": "gitlab"})
        assert p.parse_repo("o/n").host == "env-gl.example.com"

    def test_spec_context_binds_active_spec(self):
        with spec_context({"hosting": "gitlab", "gitlab_host": "ctx-gl.example.com"}):
            p = get_provider("o/n")
        assert isinstance(p, GitLabProvider)
        assert p.parse_repo("o/n").host == "ctx-gl.example.com"

    def test_spec_context_resets_after(self):
        with spec_context({"hosting": "gitlab"}):
            pass
        # outside the context the default (GitHub) applies again
        assert isinstance(get_provider("o/n"), GitHubProvider)


# --------------------------------------------------------------------------- GitLab token


class TestGitLabToken:
    def test_env_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv(gitlab_mod.TOKEN_ENV, "env-token")
        monkeypatch.setenv(gitlab_mod.TOKEN_FILE_ENV, str(tmp_path / "nope"))
        assert gitlab_mod.gitlab_token() == "env-token"

    def test_token_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv(gitlab_mod.TOKEN_ENV, raising=False)
        f = tmp_path / "token"
        f.write_text("file-token\n")
        monkeypatch.setenv(gitlab_mod.TOKEN_FILE_ENV, str(f))
        assert gitlab_mod.gitlab_token() == "file-token"

    def test_missing_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv(gitlab_mod.TOKEN_ENV, raising=False)
        monkeypatch.setenv(gitlab_mod.TOKEN_FILE_ENV, str(tmp_path / "absent"))
        with pytest.raises(HostingError):
            gitlab_mod.gitlab_token()


# --------------------------------------------------------------------------- GitLab behaviour (mocked I/O)


class TestGitLabBehaviour:
    def test_update_branch_not_supported(self):
        with pytest.raises(HostingNotSupported):
            GitLabProvider().update_branch("o/n", 1)

    def test_issue_dependencies_not_supported(self):
        with pytest.raises(HostingNotSupported):
            GitLabProvider().issue_dependencies("o/n", 1)

    def test_compare_uses_two_directions(self, monkeypatch):
        calls: list[str] = []

        def fake_request(self, method, path, payload=None, *, raw=False):
            calls.append(path)
            # forward (base->head) count 2, reverse (head->base) count 1
            return {"commit": {"count": 2 if "from=main" in path else 1}}

        monkeypatch.setattr(GitLabProvider, "_request", fake_request)
        res = GitLabProvider().compare("o/n", "main", "feat")
        assert res == CompareResult(ahead_by=2, behind_by=1)
        assert any("from=main&to=feat" in c for c in calls)
        assert any("from=feat&to=main" in c for c in calls)

    def test_compare_identical_refs_null_commit(self, monkeypatch):
        # GitLab returns {"commit": null} for an empty/identical compare — must not crash.
        monkeypatch.setattr(
            GitLabProvider, "_request", lambda self, method, path, payload=None, *, raw=False: {"commit": None}
        )
        res = GitLabProvider().compare("o/n", "main", "main")
        assert res == CompareResult(ahead_by=0, behind_by=0)

    def test_post_review_approve_vs_request_changes(self, monkeypatch):
        seen: list[tuple[str, str]] = []

        def fake_request(self, method, path, payload=None, *, raw=False):
            seen.append((method, path))

        monkeypatch.setattr(GitLabProvider, "_request", fake_request)
        p = GitLabProvider()
        p.post_review("o/n", 4, event="APPROVE")
        p.post_review("o/n", 4, event="REQUEST_CHANGES", body="fix pls")
        assert ("POST", "/projects/o%2Fn/merge_requests/4/approve") in seen
        assert ("POST", "/projects/o%2Fn/merge_requests/4/notes") in seen

    def test_edit_issue_state_maps_to_event(self, monkeypatch):
        seen: list[dict] = []

        def fake_request(self, method, path, payload=None, *, raw=False):
            seen.append(payload if payload is not None else {})
            return {"iid": 1, "state": "closed"}

        monkeypatch.setattr(GitLabProvider, "_request", fake_request)
        GitLabProvider().edit_issue("o/n", 1, state="closed")
        assert seen[0]["state_event"] == "close"


# --------------------------------------------------------------------------- GitLab API surface (mocked _request)


class _Recorder:
    """Records ``_request`` calls and returns a canned payload per path suffix."""

    def __init__(self, responses: dict[str, object] | None = None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses = responses or {}

    def __call__(self, method, path, payload=None, *, raw=False):
        # bound as an instance (not a function) → no ``self`` is passed
        self.calls.append((method, path, payload))
        path_only = path.split("?", 1)[0]
        for suffix, res in self.responses.items():
            if path_only.endswith(suffix):
                return res
        return {}


def _install(monkeypatch, rec: _Recorder) -> None:
    monkeypatch.setattr(GitLabProvider, "_request", rec)


class TestGitLabApiSurface:
    def test_create_issue_with_labels(self, monkeypatch):
        rec = _Recorder({"/issues": {"iid": 3, "title": "t"}})
        _install(monkeypatch, rec)
        out = GitLabProvider().create_issue("o/n", "t", "body", labels=["a", "b"])
        assert out.number == 3
        method, path, payload = rec.calls[0]
        assert (method, path) == ("POST", "/projects/o%2Fn/issues")
        assert payload == {"title": "t", "description": "body", "labels": "a,b"}

    def test_create_issue_without_labels(self, monkeypatch):
        rec = _Recorder({"/issues": {"iid": 3}})
        _install(monkeypatch, rec)
        GitLabProvider().create_issue("o/n", "t")
        assert rec.calls[0][2] == {"title": "t", "description": ""}

    def test_get_issue(self, monkeypatch):
        rec = _Recorder({"/issues/7": {"iid": 7, "title": "t"}})
        _install(monkeypatch, rec)
        assert GitLabProvider().get_issue("o/n", 7).number == 7

    def test_edit_issue_title_body(self, monkeypatch):
        rec = _Recorder({"/issues/7": {"iid": 7}})
        _install(monkeypatch, rec)
        GitLabProvider().edit_issue("o/n", 7, title="nt", body="nb")
        assert rec.calls[0][2] == {"title": "nt", "description": "nb"}

    def test_add_labels(self, monkeypatch):
        rec = _Recorder()
        _install(monkeypatch, rec)
        GitLabProvider().add_labels("o/n", 7, ["a", "b"])
        method, path, payload = rec.calls[0]
        # the ``.../labels`` sub-endpoint is disabled on some instances; the
        # issue update's ``add_labels`` param is the supported, idempotent way
        assert (method, path) == ("PUT", "/projects/o%2Fn/issues/7")
        assert payload == {"add_labels": "a,b"}

    def test_add_labels_empty_noop(self, monkeypatch):
        rec = _Recorder()
        _install(monkeypatch, rec)
        GitLabProvider().add_labels("o/n", 7, [])
        assert rec.calls == []

    def test_remove_label_success(self, monkeypatch):
        rec = _Recorder({"/issues/7": {"labels": ["a b", "c"]}})
        _install(monkeypatch, rec)
        assert GitLabProvider().remove_label("o/n", 7, "a b") is True
        # get current labels, then remove via the issue update's ``remove_labels`` param
        assert rec.calls[0][:2] == ("GET", "/projects/o%2Fn/issues/7")
        assert rec.calls[1][:2] == ("PUT", "/projects/o%2Fn/issues/7")
        assert rec.calls[1][2] == {"remove_labels": "a b"}

    def test_remove_label_absent_returns_false(self, monkeypatch):
        rec = _Recorder({"/issues/7": {"labels": ["c"]}})
        _install(monkeypatch, rec)
        assert GitLabProvider().remove_label("o/n", 7, "a b") is False
        assert len(rec.calls) == 1  # only the GET, no PUT

    def test_remove_label_404_returns_false(self, monkeypatch):
        import http.client
        import urllib.error

        def boom(self_, method, path, payload=None, *, raw=False):
            raise urllib.error.HTTPError(path, 404, "not found", http.client.HTTPMessage(), None)

        monkeypatch.setattr(GitLabProvider, "_request", boom)
        assert GitLabProvider().remove_label("o/n", 7, "x") is False

    def test_remove_label_other_error_raises(self, monkeypatch):
        import http.client
        import urllib.error

        def boom(self_, method, path, payload=None, *, raw=False):
            raise urllib.error.HTTPError(path, 500, "server", http.client.HTTPMessage(), None)

        monkeypatch.setattr(GitLabProvider, "_request", boom)
        with pytest.raises(urllib.error.HTTPError):
            GitLabProvider().remove_label("o/n", 7, "x")

    def test_list_issues_with_labels_and_sort(self, monkeypatch):
        rec = _Recorder({"/issues": [{"iid": 1}, {"iid": 2}]})
        _install(monkeypatch, rec)
        out = GitLabProvider().list_issues("o/n", labels=["a"], sort="created", direction="asc")
        assert [i.number for i in out] == [1, 2]
        query = rec.calls[0][1].split("?", 1)[1]
        assert "labels=a" in query and "order_by=created_at" in query and "sort=asc" in query

    def test_create_comment(self, monkeypatch):
        rec = _Recorder({"/notes": {"id": 9, "body": "hi", "author": {"username": "u"}}})
        _install(monkeypatch, rec)
        out = GitLabProvider().create_comment("o/n", 7, "hi")
        assert (out.id, out.author, out.body) == (9, "u", "hi")

    def test_list_comments(self, monkeypatch):
        rec = _Recorder({"/notes": [{"id": 1, "body": "b", "author": {"username": "u"}}]})
        _install(monkeypatch, rec)
        out = GitLabProvider().list_comments("o/n", 7)
        assert len(out) == 1 and out[0].id == 1

    def test_create_pr(self, monkeypatch):
        rec = _Recorder({"/merge_requests": {"iid": 4, "title": "t"}})
        _install(monkeypatch, rec)
        out = GitLabProvider().create_pr("o/n", title="t", head="feat", base="main", body="d")
        assert out.number == 4
        assert rec.calls[0][2] == {
            "title": "t",
            "source_branch": "feat",
            "target_branch": "main",
            "description": "d",
        }

    def test_get_pr(self, monkeypatch):
        rec = _Recorder({"/merge_requests/4": {"iid": 4, "title": "t"}})
        _install(monkeypatch, rec)
        assert GitLabProvider().get_pr("o/n", 4).number == 4

    def test_list_prs_state_labels_head(self, monkeypatch):
        rec = _Recorder({"/merge_requests": [{"iid": 1}]})
        _install(monkeypatch, rec)
        out = GitLabProvider().list_prs("o/n", state="merged", labels=["l"], head="feat")
        assert len(out) == 1
        query = rec.calls[0][1].split("?", 1)[1]
        assert "state=merged" in query and "labels=l" in query and "source_branch=feat" in query

    def test_merge_pr(self, monkeypatch):
        rec = _Recorder({"/merge": {"iid": 4, "state": "merged"}, "/merge_requests/4": {"iid": 4, "state": "merged"}})
        _install(monkeypatch, rec)
        out = GitLabProvider().merge_pr("o/n", 4, method="squash")
        assert out.number == 4
        assert rec.calls[0][0] == "PUT"

    def test_pr_files_and_diff(self, monkeypatch):
        changes = [
            {"new_path": "a.py", "diff": "+x\n-y\n", "new_file": True},
            {"new_path": "b.py", "diff": "+z\n"},
        ]
        rec = _Recorder({"/changes": {"changes": changes}})
        _install(monkeypatch, rec)
        files = GitLabProvider().pr_files("o/n", 4)
        assert [f.path for f in files] == ["a.py", "b.py"]
        assert files[0].status == "added"
        diff = GitLabProvider().pr_diff("o/n", 4)
        assert "+x" in diff and "+z" in diff

    def test_commit_files_create_delete_author(self, monkeypatch):
        rec = _Recorder({"/repository/commits": {"id": "abc123"}})
        _install(monkeypatch, rec)
        sha = GitLabProvider().commit_files(
            "o/n",
            "feat",
            "msg",
            [{"path": "a.txt", "content": "1"}, {"path": "b.txt", "deleted": True}],
            author={"name": "N", "email": "e@x"},
        )
        assert sha == "abc123"
        payload = rec.calls[0][2]
        assert payload is not None
        assert payload["branch"] == "feat"
        assert payload["actions"] == [
            {"action": "create", "file_path": "a.txt", "content": "1"},
            {"action": "delete", "file_path": "b.txt"},
        ]
        assert payload["author_name"] == "N" and payload["author_email"] == "e@x"

    def test_commit_files_no_author(self, monkeypatch):
        rec = _Recorder({"/repository/commits": {"id": "abc"}})
        _install(monkeypatch, rec)
        GitLabProvider().commit_files("o/n", "feat", "m", [{"path": "a", "content": "1"}])
        payload = rec.calls[0][2]
        assert payload is not None and "author_name" not in payload


class TestGitLabVersion:
    """Version registration and capability-driven endpoint selection (ADR 0062)."""

    def test_base_url_default_https(self):
        assert GitLabProvider()._base == "https://gitlab.com/api/v4"

    def test_base_url_override_http(self):
        # local/dev or self-hosted instances served over plain http
        p = GitLabProvider(host="localhost:8091", base_url="http://localhost:8091/api/v4/")
        assert p._base == "http://localhost:8091/api/v4"  # trailing slash stripped

    def test_parse_version_ee(self):
        from fabrik.hosting.gitlab import _parse_version

        assert _parse_version("19.5.8-ee") == (19, 5, 8)

    def test_parse_version_ce(self):
        from fabrik.hosting.gitlab import _parse_version

        assert _parse_version("14.0.1-ce") == (14, 0, 1)

    def test_parse_version_empty(self):
        from fabrik.hosting.gitlab import _parse_version

        assert _parse_version("") == (0,)

    def test_capabilities_modern(self, monkeypatch):
        rec = _Recorder({"/version": {"version": "19.5.8-ee", "enterprise": True}})
        _install(monkeypatch, rec)
        caps = GitLabProvider().capabilities()
        assert caps.version == "19.5.8-ee"
        assert caps.version_tuple == (19, 5, 8)
        assert caps.enterprise is True
        assert caps.mr_diffs_endpoint is True

    def test_capabilities_old_version(self, monkeypatch):
        rec = _Recorder({"/version": {"version": "13.11.0", "enterprise": False}})
        _install(monkeypatch, rec)
        caps = GitLabProvider().capabilities()
        assert caps.version_tuple == (13, 11, 0)
        assert caps.mr_diffs_endpoint is False

    def test_capabilities_fetched_once(self, monkeypatch):
        rec = _Recorder({"/version": {"version": "19.5.8-ee"}})
        _install(monkeypatch, rec)
        p = GitLabProvider()
        p.capabilities()
        p.capabilities()
        assert [c[1] for c in rec.calls] == ["/version"]

    def test_version_property(self, monkeypatch):
        rec = _Recorder({"/version": {"version": "19.5.8-ee"}})
        _install(monkeypatch, rec)
        assert GitLabProvider().version == "19.5.8-ee"

    def test_mr_changes_modern_endpoint(self, monkeypatch):
        diffs = [{"new_path": "a.py", "diff": "+x\n"}]
        rec = _Recorder(
            {
                "/version": {"version": "19.5.8-ee"},
                "/versions": [{"id": 99, "state": "collected"}, {"id": 100, "state": "collected"}],
                "/merge_request_diffs/100": {"diffs": diffs},
            }
        )
        _install(monkeypatch, rec)
        out = GitLabProvider()._mr_changes("o/n", 4)
        assert [c["new_path"] for c in out] == ["a.py"]
        paths = [c[1] for c in rec.calls]
        assert "/projects/o%2Fn/merge_requests/4/versions" in paths
        assert "/projects/o%2Fn/merge_request_diffs/100" in paths  # newest diff id

    def test_mr_changes_fallback_on_404(self, monkeypatch):
        import http.client
        import urllib.error

        changes = [{"new_path": "a.py", "diff": "+x\n"}]

        def fake(self_, method, path, payload=None, *, raw=False):
            if path.endswith("/merge_request_diffs/100"):
                raise urllib.error.HTTPError(path, 404, "not found", http.client.HTTPMessage(), None)
            if path.endswith("/versions"):
                return [{"id": 100}]
            if path.endswith("/changes"):
                return {"changes": changes}
            return {}

        monkeypatch.setattr(GitLabProvider, "_request", fake)
        out = GitLabProvider()._mr_changes("o/n", 4)
        assert [c["new_path"] for c in out] == ["a.py"]

    def test_mr_changes_polls_until_collected(self, monkeypatch):
        import fabrik.hosting.gitlab as gl

        monkeypatch.setattr(gl.time, "sleep", lambda s: None)
        state = {"n": 0}

        def fake(self_, method, path, payload=None, *, raw=False):
            if path.endswith("/changes"):
                state["n"] += 1
                # the diff is computed asynchronously: empty on the first poll
                return {"changes": [] if state["n"] == 1 else [{"new_path": "a.py", "diff": "+x\n"}]}
            return {}  # /version → empty → old version → deprecated /changes path

        monkeypatch.setattr(GitLabProvider, "_request", fake)
        out = GitLabProvider()._mr_changes("o/n", 4)
        assert state["n"] == 2
        assert [c["new_path"] for c in out] == ["a.py"]


class TestGitLabNormalizationExtras:
    def test_comment_from_missing_author(self):
        from fabrik.hosting.gitlab import _comment_from

        out = _comment_from({"id": 1, "body": "b"})
        assert out.author == ""

    def test_file_from_renamed(self):
        from fabrik.hosting.gitlab import _file_from

        assert _file_from({"new_path": "a.py", "diff": "", "renamed_file": True}).status == "renamed"

    def test_file_from_modified(self):
        from fabrik.hosting.gitlab import _file_from

        assert _file_from({"new_path": "a.py", "diff": ""}).status == "modified"

    def test_parse_repo_ssh_unparseable(self):
        with pytest.raises(HostingConfigError):
            GitLabProvider().parse_repo("git@h.co:onlyowner")


# --------------------------------------------------------------------------- GitLab webhook parsing


class TestGitLabWebhook:
    def _mr(self, action="open", iid=12):
        return (
            {"X-Gitlab-Event": "Merge Request Hook"},
            (
                b'{"object_attributes": {"action": "'
                + action.encode()
                + b'", "iid": '
                + str(iid).encode()
                + b', "title": "t", "url": "https://u"},'
                b'"project": {"path_with_namespace": "o/n"}}'
            ),
        )

    def test_mr_hook(self):
        headers, body = self._mr()
        ev = GitLabProvider().parse_webhook(headers, body)
        assert ev is not None
        assert (ev.kind, ev.action, ev.number, ev.repo) == ("pr", "open", 12, "o/n")

    def test_issue_hook(self):
        headers, body = self._mr()
        headers = {"X-Gitlab-Event": "Issue Hook"}
        ev = GitLabProvider().parse_webhook(headers, body)
        assert ev is not None and ev.kind == "issue"

    def test_unknown_event_returns_none(self):
        headers, body = self._mr()
        headers = {"X-Gitlab-Event": "Push Hook"}
        assert GitLabProvider().parse_webhook(headers, body) is None

    def test_bad_json_returns_none(self):
        headers, _ = self._mr()
        assert GitLabProvider().parse_webhook(headers, b"not-json") is None

    def test_missing_project_returns_none(self):
        headers = {"X-Gitlab-Event": "Merge Request Hook"}
        assert GitLabProvider().parse_webhook(headers, b'{"object_attributes": {"iid": 1}}') is None


# --------------------------------------------------------------------------- GitHub compare (unknown counts)


class TestGitHubCompare:
    def test_missing_counts_are_none(self, monkeypatch):
        # GitHub compare with no counts → None ("unknown"), not 0 — the pr-step relies on this.
        monkeypatch.setattr(GitHubProvider, "_api", lambda self, method, path, payload=None: {})
        res = GitHubProvider().compare("o/n", "main", "feat")
        assert res.ahead_by is None and res.behind_by is None

    def test_counts_passthrough(self, monkeypatch):
        monkeypatch.setattr(
            GitHubProvider, "_api", lambda self, method, path, payload=None: {"ahead_by": 3, "behind_by": 1}
        )
        res = GitHubProvider().compare("o/n", "main", "feat")
        assert (res.ahead_by, res.behind_by) == (3, 1)


# --------------------------------------------------------------------------- dataclass sanity


def test_repo_ref_slug_property():
    assert RepoRef("h", "o", "n").slug == "o/n"


def test_issue_defaults():
    i = Issue(number=1, title="t", body="", state="open", labels=[], url="")
    assert i.is_pr is False and i.raw == {}


def test_pull_request_defaults():
    p = PullRequest(
        number=1, title="t", body="", state="open", labels=[], url="", head_ref="", head_sha="", base_ref=""
    )
    assert p.mergeable is None and p.raw == {}


# --------------------------------------------------------------------------- registry (ADR 0063)


@pytest.fixture
def clean_registry():
    """Snapshot and restore the provider registry around a test (it is global state)."""
    snapshot = dict(registry_mod._REGISTRY)
    yield
    registry_mod._REGISTRY.clear()
    registry_mod._REGISTRY.update(snapshot)


class TestRegistry:
    def test_builtin_providers_registered(self):
        names = registry_mod.registered_providers()
        assert "github" in names and "gitlab" in names

    def test_register_and_get(self, clean_registry):
        factory = lambda repo, spec, role: "custom"  # noqa: E731
        registry_mod.register_provider("testhost", factory)
        assert registry_mod.get_factory("testhost") is factory

    def test_register_empty_name_raises(self, clean_registry):
        with pytest.raises(HostingConfigError):
            registry_mod.register_provider("   ", lambda r, s, role: None)

    def test_register_non_callable_raises(self, clean_registry):
        with pytest.raises(HostingConfigError):
            registry_mod.register_provider("testhost", "not-callable")

    def test_register_duplicate_raises(self, clean_registry):
        registry_mod.register_provider("testhost", lambda r, s, role: None)
        with pytest.raises(HostingConfigError):
            registry_mod.register_provider("testhost", lambda r, s, role: None)

    def test_register_duplicate_replace(self, clean_registry):
        first = lambda r, s, role: "first"  # noqa: E731
        second = lambda r, s, role: "second"  # noqa: E731
        registry_mod.register_provider("testhost", first)
        registry_mod.register_provider("testhost", second, replace=True)
        assert registry_mod.get_factory("testhost") is second

    def test_get_factory_unknown_raises_with_known_names(self, clean_registry):
        with pytest.raises(HostingConfigError) as exc:
            registry_mod.get_factory("nope")
        assert "github" in str(exc.value)

    def test_names_are_case_insensitive(self, clean_registry):
        registry_mod.register_provider("MyHost", lambda r, s, role: None)
        assert registry_mod.get_factory("myhost") is not None
        assert "myhost" in registry_mod.registered_providers()

    def test_get_provider_uses_registered_name(self, clean_registry):
        sentinel = object()
        registry_mod.register_provider("customhost", lambda r, s, role: sentinel)
        assert get_provider("o/n", spec={"hosting": "customhost"}) is sentinel

    def test_get_provider_unknown_hosting_raises(self):
        with pytest.raises(HostingConfigError, match="gitlabb.*registered: .*gitlab"):
            get_provider("o/n", spec={"hosting": "gitlabb"})

    def test_load_entry_points_warns_on_broken_plugin(self, clean_registry, monkeypatch, caplog):
        class _EP:
            name = "broken"

            def load(self):
                raise ImportError("boom")

        class _EPs:
            def select(self, group):
                return [_EP()]

        import importlib.metadata as md

        monkeypatch.setattr(md, "entry_points", lambda: _EPs())
        with caplog.at_level("WARNING"):
            registry_mod.load_entry_points()
        assert "broken" in caplog.text and "boom" in caplog.text
        assert "broken" not in registry_mod.registered_providers()

    def test_has_credentials(self, monkeypatch):
        monkeypatch.setattr(gitops, "_gh_token_safe", lambda role="dev": "")
        assert get_provider("o/n").has_credentials() is False
        monkeypatch.setattr(gitops, "_gh_token_safe", lambda role="dev": "t")
        assert get_provider("o/n").has_credentials() is True
        monkeypatch.setenv("FABRIK_GITLAB_TOKEN", "")
        monkeypatch.setenv("FABRIK_GITLAB_TOKEN_FILE", "/nonexistent/token")
        assert get_provider("o/n", spec={"hosting": "gitlab"}).has_credentials() is False
        monkeypatch.setenv("FABRIK_GITLAB_TOKEN", "x")
        assert get_provider("o/n", spec={"hosting": "gitlab"}).has_credentials() is True


# --------------------------------------------------------------------------- BaseProvider (ADR 0063)


class _MinimalProvider(BaseProvider):
    """A minimal adapter implementing only the core ops (parse_repo + issues + PRs)."""

    name = "minimal"

    def parse_repo(self, ref):
        owner, _, name = ref.partition("/")
        return RepoRef("minimal.example", owner, name)

    def create_issue(self, ref, title, body="", labels=None):
        return Issue(number=1, title=title, body=body, state="open", labels=labels or [], url="")

    def get_issue(self, ref, number):
        return Issue(number=number, title="t", body="", state="open", labels=[], url="")

    def edit_issue(self, ref, number, *, title=None, body=None, state=None, state_reason=None, duplicate_issue_id=None):
        return self.get_issue(ref, number)

    def create_pr(self, ref, *, title, head, base, body=""):
        return PullRequest(
            number=1, title=title, body=body, state="open", labels=[], url="", head_ref=head, head_sha="", base_ref=base
        )

    def get_pr(self, ref, number):
        return PullRequest(
            number=number, title="t", body="", state="open", labels=[], url="", head_ref="", head_sha="", base_ref=""
        )

    def merge_pr(self, ref, number, *, method="squash", sha=None):
        return self.get_pr(ref, number)


class TestBaseProvider:
    def test_core_ops_are_implemented(self):
        p = _MinimalProvider()
        assert p.get_issue("o/n", 3).number == 3
        assert p.get_pr("o/n", 4).number == 4

    def test_optional_ops_raise_by_default(self):
        p = _MinimalProvider()
        for call in (
            lambda: p.add_labels("o/n", 1, ["x"]),
            lambda: p.remove_label("o/n", 1, "x"),
            lambda: p.list_issues("o/n"),
            lambda: p.create_comment("o/n", 1, "b"),
            lambda: p.list_comments("o/n", 1),
            lambda: p.list_prs("o/n"),
            lambda: p.update_branch("o/n", 1),
            lambda: p.pr_files("o/n", 1),
            lambda: p.pr_diff("o/n", 1),
            lambda: p.post_review("o/n", 1, event="APPROVE"),
            lambda: p.compare("o/n", "a", "b"),
            lambda: p.issue_dependencies("o/n", 1),
            lambda: p.commit_files("o/n", "b", "m", []),
        ):
            with pytest.raises(HostingNotSupported):
                call()

    def test_override_enables_op(self):
        class _WithBranch(_MinimalProvider):
            def update_branch(self, ref, number, *, expected_head_sha=None):
                return "ok"

        assert _WithBranch().update_branch("o/n", 1) == "ok"
