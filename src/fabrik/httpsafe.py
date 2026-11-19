"""``urlopen`` nur für http(s): URLs aus Spec/Config/Env dürfen nicht auf ``file:`` o. Ä. zeigen.

Zentrale Stelle statt verstreuter ``# nosec B310`` (Bandit/SAST): jeder HTTP-Call der Fabrik
läuft hierüber, die Scheme-Prüfung passiert genau einmal.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from http.client import HTTPResponse

from .failures import ConfigError
from .i18n import t

ALLOWED_SCHEMES = frozenset({"http", "https"})


def urlopen(req: urllib.request.Request | str, timeout: float) -> HTTPResponse:
    """Wie ``urllib.request.urlopen``, aber nur für ``http``/``https``.

    Raises:
        ConfigError: anderes Scheme (``file:``, ``ftp:``, ohne Scheme) — Konfigurationsfehler, kein Netz-Flake.
    """
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ConfigError(t("httpsafe.url_scheme_not_allowed_http", v1=scheme or "(leer)", url=url))
    # Attribut-Lookup zur Laufzeit (nicht `from urllib.request import urlopen`): Tests patchen urllib.request.urlopen.
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310 # Scheme oben geprüft
