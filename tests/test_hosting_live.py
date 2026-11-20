"""Live GitLab checks for the hosting provider (ADR 0062) — skipped by default.

Run explicitly against a real instance:

    FABRIK_GITLAB_LIVE=1 FABRIK_GITLAB_HOST=gitlab.opencode.de \
        FABRIK_GITLAB_LIVE_REF=oc000163725684/test-project \
        .venv/bin/python -m pytest tests/test_hosting_live.py -v

The token must be available via ``FABRIK_GITLAB_TOKEN`` or ``~/.config/gitlab/token``.

These checks are read-only. The verification PAT carries the ``api`` scope, but the
instance answers project creation with ``403 insufficient_scope`` ("the request requires
higher privileges than provided by the access token"), so the create/commit/MR/merge flow
cannot be exercised with it. The read path (auth, project, issue/PR lists, compare, webhook
parsing) is what is verified here.
"""

from __future__ import annotations

import os

import pytest

from fabrik.hosting import GitLabProvider

LIVE = os.environ.get("FABRIK_GITLAB_LIVE", "").strip().lower() in ("1", "true", "yes")
HOST = os.environ.get("FABRIK_GITLAB_HOST", "gitlab.opencode.de")
REF = os.environ.get("FABRIK_GITLAB_LIVE_REF", "oc000163725684/test-project")

pytestmark = pytest.mark.skipif(not LIVE, reason="set FABRIK_GITLAB_LIVE=1 to run live GitLab checks")


def test_live_auth_and_repo_parse():
    p = GitLabProvider(host=HOST)
    me = p._request("GET", "/user")
    assert me.get("state") == "active"
    r = p.parse_repo(REF)
    assert r.host == HOST and r.slug == REF


def test_live_project_and_reads():
    p = GitLabProvider(host=HOST)
    proj = p._request("GET", f"/projects/{p._pid(REF)}")
    assert proj.get("default_branch")
    issues = p.list_issues(REF, state="all")
    prs = p.list_prs(REF, state="all")
    assert isinstance(issues, list) and isinstance(prs, list)


def test_live_compare_identical_refs():
    # GitLab returns {"commit": null} for an identical compare — must normalize to 0/0.
    p = GitLabProvider(host=HOST)
    res = p.compare(REF, "main", "main")
    assert res.ahead_by == 0 and res.behind_by == 0


def test_live_webhook_parse():
    ev = GitLabProvider(host=HOST).parse_webhook(
        {"X-Gitlab-Event": "Merge Request Hook"},
        b'{"object_attributes": {"action": "open", "iid": 1, "title": "t", "url": "u"},'
        b'"project": {"path_with_namespace": "o/n"}}',
    )
    assert ev is not None and (ev.kind, ev.repo, ev.action) == ("pr", "o/n", "open")
