"""scripts/gh-upsert-comment.sh: ein Kommentar pro Marker (Update statt Spam), Token der richtigen App."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gh-upsert-comment.sh"
EX_CONFIG = 78
MARKER = "<!-- fabrikant-sonar -->"


def _run(
    tmp_path: Path,
    *,
    existing_id: str = "",
    env_file: str | None = None,
    args: list[str] | None = None,
    user_login: str = "",
):
    """`gh`-Stub: protokolliert Aufrufe + GH_TOKEN; die Kommentar-Liste liefert `existing_id`,
    `api user` den Login (leer = App-Token, /user verboten)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "gh.log"
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'echo "token=$GH_TOKEN args=$*" >> "{log}"\n'
        f'case "$*" in "api user"*) [ -n "{user_login}" ] && echo "{user_login}" || exit 1;; '
        f'*--paginate*) echo "{existing_id}";; *) echo https://example/c;; esac\n'
    )
    gh.chmod(0o755)
    tokens = tmp_path / "github.env"
    tokens.write_text("GITHUB_TOKEN=builder\nGITHUB_REVIEWER_TOKEN='reviewer'\n")
    body = tmp_path / "comment.md"
    body.write_text("### Gate ✅\n")
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FABRIKANT_ENV_FILE": env_file if env_file is not None else str(tokens),
    }
    env.pop("FABRIKANT_PROJECT_ENV_FILE", None)
    env.pop("FABRIKANT_REVIEWER_LOGIN", None)
    argv = args if args is not None else ["o/r", "7", MARKER, str(body)]
    p = subprocess.run(["bash", str(SCRIPT), *argv], env=env, capture_output=True, text=True, timeout=30)
    return p, (log.read_text() if log.exists() else "")


def test_creates_comment_as_reviewer_when_none_exists(tmp_path):
    p, log = _run(tmp_path)
    assert p.returncode == 0, p.stderr
    # Reviewer-App, nicht das Builder-Token; neuer Kommentar statt Update.
    assert "token=reviewer args=api repos/o/r/issues/7/comments -f body=" in log
    assert "PATCH" not in log
    assert f"body={MARKER}\n### Gate" in log  # Marker vorangestellt


def test_updates_existing_bot_comment(tmp_path):
    p, log = _run(tmp_path, existing_id="4711")
    assert p.returncode == 0, p.stderr
    assert "token=reviewer args=api -X PATCH repos/o/r/issues/comments/4711 -f body=" in log


@pytest.mark.parametrize(
    ("env_file", "args"),
    [
        ("/nonexistent/github.env", None),  # Token-Datei fehlt
        (None, ["o/r", "abc", MARKER, "/dev/null"]),  # keine PR-Nummer
        (None, ["o/r", "7"]),  # Aufruf unvollständig
    ],
)
def test_config_errors_exit_78_without_api_calls(tmp_path, env_file, args):
    p, log = _run(tmp_path, env_file=env_file, args=args)
    assert p.returncode == EX_CONFIG, p.stdout + p.stderr
    assert log == ""


@pytest.mark.parametrize(
    ("user_login", "expected_filter"),
    [
        ("octocat", '.user.login == "octocat"'),  # PAT: nur eigene Kommentare, auch ohne Bot-Typ
        ("", '.user.type == "Bot"'),  # App-Token: /user verboten → Bot-Kommentare
    ],
)
def test_only_own_comments_are_updated(tmp_path, user_login, expected_filter):
    p, log = _run(tmp_path, user_login=user_login)
    assert p.returncode == 0, p.stderr
    listing = next(line for line in log.splitlines() if "--paginate" in line)
    assert expected_filter in listing
