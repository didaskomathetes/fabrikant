"""Input validation for values that go to git and the GitHub API."""

from __future__ import annotations

import re

from .i18n import t

# Git allows more, but branch names partly come from LLM/user input:
# deliberately a narrow allowlist instead of a blocklist for shell/ref metacharacters.
MAX_BRANCH_NAME_LEN = 200
_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")


def validate_branch_name(branch: str) -> str:
    """Check a branch/head name and return it unchanged; otherwise `ValueError`.

    Rejected: empty, too long (`MAX_BRANCH_NAME_LEN`), characters outside
    `[A-Za-z0-9._/-]` (spaces, newlines, shell metacharacters),
    leading `-` (option injection), `..`, `//`, `/`/`.` edge segments
    and `.lock` suffix (invalid git refs).
    """
    if not isinstance(branch, str) or not branch:
        raise ValueError(t("validation.branch_name_is_empty"))
    if len(branch) > MAX_BRANCH_NAME_LEN:
        raise ValueError(t("validation.branch_name_too_long_characters", v1=MAX_BRANCH_NAME_LEN))
    if not _BRANCH_RE.fullmatch(branch):
        raise ValueError(t("validation.branch_name_contains_illegal_characters", branch=branch))
    if branch.startswith("-"):
        raise ValueError(t("validation.branch_name_must_not_start", branch=branch))
    if ".." in branch or "//" in branch:
        raise ValueError(t("validation.branch_name_contains_or", branch=branch))
    if any(not seg or seg.startswith(".") or seg.endswith(".lock") for seg in branch.split("/")) or branch.endswith(
        "."
    ):
        raise ValueError(t("validation.branch_name_has_an_invalid", branch=branch))
    return branch
