"""Konstanten in web/app.js, die Server-Definitionen spiegeln, bleiben mit ihnen im Gleichstand."""

import json
import re

import pytest

from fabrik import config
from fabrik.runmodel import FAILED_RUN_STATUSES, FAILED_STEP_STATUSES


def _js_string_array(name: str) -> set[str]:
    src = (config.WEB_DIR / "app.js").read_text(encoding="utf-8")
    m = re.search(rf"^const {name} = (\[[^\]]*\]);", src, re.MULTILINE)
    assert m, f"{name} nicht in web/app.js gefunden"
    return set(json.loads(m.group(1)))


@pytest.mark.parametrize(
    ("js_name", "server"),
    [("FAILED_RUN_STATUSES", FAILED_RUN_STATUSES), ("FAILED_STEP_STATUSES", FAILED_STEP_STATUSES)],
)
def test_failed_statuses_match_runmodel(js_name, server):
    assert _js_string_array(js_name) == {s.value for s in server}
