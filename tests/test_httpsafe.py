"""fabrikant.httpsafe: urlopen nur für http(s) (Bandit B310)."""

from __future__ import annotations

import urllib.request

import pytest

from fabrikant import httpsafe
from fabrikant.failures import ConfigError
from fabrikant.runmodel import FailureKind


@pytest.mark.parametrize("url", ["http://x/a", "https://x/a", "HTTPS://x/a"])
def test_http_schemes_are_opened(monkeypatch, url):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: seen.append((req, timeout)) or "resp")
    req = urllib.request.Request(url)
    assert httpsafe.urlopen(req, timeout=5) == "resp"
    assert seen == [(req, 5)]


def test_plain_string_url_is_accepted(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: "resp")
    assert httpsafe.urlopen("https://x", timeout=1) == "resp"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/a", "/relative/path"])
def test_other_schemes_are_config_errors(monkeypatch, url):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("darf nicht öffnen"))
    with pytest.raises(ConfigError, match="nicht erlaubt") as exc:
        httpsafe.urlopen(url, timeout=1)
    assert exc.value.failure.kind is FailureKind.ENVIRONMENT
