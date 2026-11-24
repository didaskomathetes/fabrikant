"""run_meta: Issue/PR-Links aus Variablen, Approval-Kontext und Trigger."""

from fabrikant.runmeta import run_meta

SPEC = {"github": "o/r"}


def test_issue_trigger_with_vars_and_pr_url():
    rec = {
        "trigger": "on_issue:#290",
        "vars": {
            "issue_number": "290",
            "issue_title": "T",
            "pr_url": "https://github.com/o/r/pull/7",
            "pr_number": "7",
        },
    }
    meta = run_meta(SPEC, rec)
    assert meta["issue"] == {"number": "290", "title": "T", "url": "https://github.com/o/r/issues/290"}
    assert meta["pr"]["url"] == "https://github.com/o/r/pull/7"
    assert meta["pr"]["number"] == "7"


def test_pr_trigger_only():
    meta = run_meta(SPEC, {"trigger": "on_pr:#5@abc"})
    assert meta == {"pr": {"number": "5", "title": "", "url": "https://github.com/o/r/pull/5"}}


def test_pr_from_approval_without_repo():
    rec = {"trigger": "manual", "approval": {"pr": "https://x/pull/9", "pr_title": "Fix"}}
    assert run_meta(None, rec) == {"pr": {"number": "9", "title": "Fix", "url": "https://x/pull/9"}}


def test_nothing():
    assert run_meta(SPEC, {"trigger": "manual"}) == {}
