"""Catch network blips (DNS, dropped connections) at the source instead of losing the run.

A short DNS outage (e.g. Wi-Fi that resets its DNS servers) made runs fail whose work was long
done — for example at the push after green tests. Network calls therefore retry with growing
waits (`config.NET_RETRY_DELAYS_S`) as long as the error is transient; only after that does the
failure class `transient` and with it the run retry take over (ADR 0035/0036).

HTTP calls replay only what is safe to send twice (:func:`may_replay`): a timeout or reset can hit
after the server already committed a POST (a new issue or comment would then exist twice).
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
import urllib.error
import urllib.parse
from collections.abc import Callable, Sequence

from . import config
from .failures import FailureKind, classify_git_output
from .i18n import t

log = logging.getLogger(__name__)

# Methods whose replay leaves the same state: HTTP-idempotent ones plus PATCH — every PATCH fabrikant
# sends sets absolute values (issue state/body/labels, comment/PR body, a ref's sha), none appends.
REPLAY_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"})


def delays(override: Sequence[float] | None = None) -> tuple[float, ...]:
    """Waits between the attempts (length = number of retries)."""
    return tuple(config.NET_RETRY_DELAYS_S if override is None else override)


# POSTs without a side effect a replay could repeat: GitHub's content-addressed git objects (the same
# content yields the same sha, nothing references it until a ref moves) and GraphQL queries (reads).
_REPLAY_SAFE_POST_PATHS = ("/git/blobs", "/git/trees")


def _replay_safe_post(url: str, data: object) -> bool:
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    if path.endswith(_REPLAY_SAFE_POST_PATHS):
        return True
    if not path.endswith("/graphql") or not isinstance(data, bytes):
        return False
    try:
        query = json.loads(data).get("query")
    except (ValueError, AttributeError):
        return False
    # Anonymous `{ … }` or `query …` reads; `mutation` (and anything unexpected) is not replayed.
    return isinstance(query, str) and query.lstrip().startswith(("{", "query"))


def may_replay(method: str, exc: BaseException, url: str = "", data: object = None) -> bool:
    """Whether a failed HTTP request may be sent again. Replay-safe methods: yes. Others (POST): only if
    the request has no side effect a replay could repeat (git blobs/trees, GraphQL queries — needs `url`
    and `data`) or provably never reached the server — name resolution failed or the connection was
    refused (urllib wraps errors while connecting as `URLError`). Timeouts and resets are ambiguous for
    other writes: the server may have committed them before the answer got lost."""
    if method.upper() in REPLAY_SAFE_METHODS or (url and _replay_safe_post(url, data)):
        return True
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else None
    return isinstance(reason, (socket.gaierror, ConnectionRefusedError))


def run_git(
    cmd: list[str],
    *,
    timeout: float,
    text: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> subprocess.CompletedProcess:
    """`subprocess.run` for a git network command (fetch, push): retry on transient stderr
    (:func:`classify_git_output`). Returns the last result — the caller checks `returncode` as
    usual. Fetch/push are repeatable (a push that already went through reports "up to date" the
    second time)."""
    waits = delays()
    for attempt in range(len(waits) + 1):
        p = subprocess.run(cmd, capture_output=True, text=text, timeout=timeout)
        if p.returncode == 0 or attempt == len(waits):
            return p
        stderr = p.stderr if text else p.stderr.decode(errors="replace")
        if classify_git_output(stderr) is not FailureKind.TRANSIENT:
            return p
        # Log only the subcommand: fetch URLs can contain a token.
        sub = cmd[3] if len(cmd) > 3 and cmd[1] == "-C" else cmd[1]
        log.warning(t("netretry.git_transient_retry"), sub, attempt + 2, waits[attempt])
        sleep(waits[attempt])
    raise AssertionError("unreachable")  # pragma: no cover
