# ruff: noqa: F811  (fixtures imported from test_ui_browser)
"""Browser tests: spec-proposal badge, diff modal, apply/reject (ADR 0066)."""

from __future__ import annotations

import pytest
from test_ui_browser import fabrikant_home, fabrikant_url, page  # noqa: F401

from fabrikant import spec_proposals as sp
from fabrikant import specs

pytestmark = pytest.mark.browser

NEW_LOOP = {"steps": [{"type": "shell", "cmd": "echo hi", "env": {"API_TOKEN": "s3cret-value"}}]}


def _propose(fabrikant_home, patch):
    path = fabrikant_home / "projects" / "uitest.fabrikant.yaml"
    return sp.propose("uitest", patch, specs.spec_hash(path), "mcp")


def _open_modal(page):
    page.evaluate("refresh()")
    page.wait_for_selector("#projects .propbadge")
    page.click("#projects .propbadge")
    page.click("#projects .applist .apprline")
    page.wait_for_selector("#proposalpanel:not(.hidden) .propsummary")


def test_proposal_badge_list_and_diff_modal(page, fabrikant_home):
    _propose(fabrikant_home, {"loops": {"newloop": NEW_LOOP, "lint": {"description": "changed"}}})
    _open_modal(page)
    assert "1 Loop(s) hinzugefügt, 1 geändert, 0 entfernt" in page.inner_text("#proposalbody .propsummary")
    assert page.locator("#proposalbody .proploop").count() == 2
    page.click("#proposalbody .proploop:has-text('lint') summary")
    assert page.locator("#proposalbody .propdiff .add").count() >= 1
    assert page.locator("#proposalbody .propdiff .del").count() >= 1
    assert "s3cret-value" not in page.inner_text("#proposalbody")


def test_apply_from_modal_updates_spec_and_badge(page, fabrikant_home):
    pr = _propose(fabrikant_home, {"loops": {"newloop": NEW_LOOP}})
    _open_modal(page)
    page.once("dialog", lambda d: d.accept())
    page.click("[data-act=prop-apply]")
    page.wait_for_selector("#proposalpanel.hidden", state="attached")
    page.wait_for_function("document.querySelector('#projects .propbadge') === null")
    assert sp.get_proposal(pr.id).status is sp.ProposalStatus.APPLIED


def test_stale_proposal_disables_apply_and_reject_works(page, fabrikant_home):
    pr = _propose(fabrikant_home, {"loops": {"newloop": NEW_LOOP}})
    specs.save_spec("uitest", specs.SpecUpdate(project_fields={"description": "moved"}))
    _open_modal(page)
    assert page.locator(".proposal-warn").count() == 1
    assert page.is_disabled("[data-act=prop-apply]")
    page.once("dialog", lambda d: d.accept())
    page.click("[data-act=prop-reject]")
    page.wait_for_function("document.querySelector('#projects .propbadge') === null")
    assert sp.get_proposal(pr.id).status is sp.ProposalStatus.REJECTED
