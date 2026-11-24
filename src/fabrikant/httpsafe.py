"""``urlopen`` for http(s) only: URLs from spec/config/env must not point to ``file:`` or similar.

Central place instead of scattered ``# nosec B310`` (Bandit/SAST): every HTTP call of the factory
goes through here, the scheme check happens exactly once.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from http.client import HTTPResponse

from .failures import ConfigError
from .i18n import t

ALLOWED_SCHEMES = frozenset({"http", "https"})


def urlopen(req: urllib.request.Request | str, timeout: float) -> HTTPResponse:
    """Like ``urllib.request.urlopen``, but only for ``http``/``https``.

    Raises:
        ConfigError: other scheme (``file:``, ``ftp:``, no scheme) — configuration error, not a network flake.
    """
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ConfigError(t("httpsafe.url_scheme_not_allowed_http", v1=scheme or "(leer)", url=url))
    # Attribute lookup at runtime (not `from urllib.request import urlopen`): tests patch urllib.request.urlopen.
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310 # scheme checked above
