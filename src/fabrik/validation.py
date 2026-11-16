"""Eingabe-Validierung für Werte, die an Git und die GitHub-API gehen."""

from __future__ import annotations

import re

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
        raise ValueError("Branch-Name ist leer")
    if len(branch) > MAX_BRANCH_NAME_LEN:
        raise ValueError(f"Branch-Name zu lang (> {MAX_BRANCH_NAME_LEN} Zeichen)")
    if not _BRANCH_RE.fullmatch(branch):
        raise ValueError(f"Branch-Name enthält unzulässige Zeichen: {branch!r}")
    if branch.startswith("-"):
        raise ValueError(f"Branch-Name darf nicht mit '-' beginnen: {branch!r}")
    if ".." in branch or "//" in branch:
        raise ValueError(f"Branch-Name enthält '..' oder '//': {branch!r}")
    if any(not seg or seg.startswith(".") or seg.endswith(".lock") for seg in branch.split("/")) or branch.endswith(
        "."
    ):
        raise ValueError(f"Branch-Name hat ungültiges Segment: {branch!r}")
    return branch
