"""Unit tests for the hosting-provider abstraction (ADR 0062).

Covers the pure contract logic (repo parsing, payload normalization, provider
selection, GitLab token resolution, webhook parsing) and the documented
provider gaps — no network (adapter I/O is mocked where exercised).
"""

from __future__ import annotations

import pytest

from fabrik.hosting import (
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
from fabrik.hosting import select as select_mod

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
        monkeypatch.setenv(select_mod.GITLAB_HOST_ENV, "env-gl.example.com")
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
