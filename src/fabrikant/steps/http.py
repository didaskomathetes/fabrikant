"""Step: http (urllib, method/headers/body/expect/emit → variable store)."""

from __future__ import annotations

import urllib.error
import urllib.request

from .. import httpsafe
from ..context import _cap, _substitute
from ..failures import ConfigError, classify_http, failed
from ..i18n import t


def _step_http(ctx: dict, step: dict, emit) -> dict:
    """HTTP request (urllib); response/status → variable store (emit)."""
    url = _substitute(step["url"], ctx)
    method = str(step.get("method", "GET")).upper()
    headers = {k: _substitute(str(v), ctx) for k, v in (step.get("headers") or {}).items()}
    body = step.get("body")
    data = _substitute(str(body), ctx).encode() if body is not None else None
    timeout = int(step.get("timeout", 30))
    emit(f"  {method} {url}")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with httpsafe.urlopen(req, timeout=timeout) as r:
            status, content, resp_headers = r.status, r.read().decode(errors="replace"), r.headers
    except urllib.error.HTTPError as e:
        status, content, resp_headers = e.code, e.read().decode(errors="replace"), e.headers
    expect = step.get("expect") or {}
    want = expect.get("status", 200)
    if status != want:
        return failed(
            t("steps.http.http_expected", status=status, want=want),
            classify_http(status, resp_headers),
            output=_cap(content),
        )
    for var, kind in (step.get("emit") or {}).items():
        if kind == "status":
            ctx[var] = str(status)
        elif kind == "body":
            ctx[var] = content
        else:
            raise ConfigError(f"http emit: unknown type '{kind}' (body|status)")
    return {"ok": True, "output": _cap(content), "summary": f"HTTP {status}, {len(content)} chars"}
