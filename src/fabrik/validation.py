"""Eingabe-Validierung für Werte, die an Git und die GitHub-API gehen."""

from __future__ import annotations

import re

from .i18n import t

# Git erlaubt mehr, aber Branch-Namen kommen teils aus LLM-/User-Eingaben:
# bewusst enge Allowlist statt Blocklist für Shell-/Ref-Metazeichen.
MAX_BRANCH_NAME_LEN = 200
_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")


def validate_branch_name(branch: str) -> str:
    """Branch-/Head-Name prüfen und unverändert zurückgeben; sonst `ValueError`.

    Abgelehnt: leer, zu lang (`MAX_BRANCH_NAME_LEN`), Zeichen außerhalb von
    `[A-Za-z0-9._/-]` (Leerzeichen, Zeilenumbrüche, Shell-Metazeichen),
    führendes `-` (Option-Injection), `..`, `//`, `/`-/`.`-Rand-Segmente
    und `.lock`-Suffix (ungültige Git-Refs).
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
