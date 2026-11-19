"""Token-Zähler eines Agent-/LLM-Calls mit Cache-Aufschlüsselung.

``input`` ist der **gesamte** Prompt-Verbrauch inklusive Cache-Anteil (OpenAI-Semantik von
``prompt_tokens``). ``cache_read``/``cache_write`` sind Teilmengen davon: aus dem Prompt-Cache gelesen
bzw. in ihn geschrieben. Ein Agent liest seinen Kontext in jedem Turn neu — der Großteil von
``input`` ist bei langen Sessions billiger Cache-Read; ``fresh_input`` ist der Rest.

Zwei Formen an den Grenzen: das Poll-Protokoll der Backends (``{"input", "output", "cache_read",
"cache_write"}``) und die Step-Usage (``prompt_tokens``, ``completion_tokens``, ``total_tokens``,
``cache_read_tokens``, ``cache_write_tokens``). ``0`` heißt auch „Backend meldet es nicht“.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_THOUSAND = 1_000
_MILLION = 1_000_000


def as_count(value: Any) -> int:
    """Nicht-negativer int aus einem JSON-Wert; alles andere → 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def compact(n: int) -> str:
    """Kurzform für Logs: ``950``, ``12.3k``, ``1.30M``."""
    if n >= _MILLION:
        return f"{n / _MILLION:.2f}M"
    if n >= _THOUSAND:
        return f"{n / _THOUSAND:.1f}k"
    return str(n)


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """Token-Verbrauch; ``cache_read``/``cache_write`` sind in ``input`` enthalten."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __add__(self, other: TokenCounts) -> TokenCounts:
        return TokenCounts(
            self.input + other.input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_write + other.cache_write,
        )

    @property
    def fresh_input(self) -> int:
        """Prompt-Tokens, die nicht aus dem Cache kamen (inkl. Cache-Writes)."""
        return max(self.input - self.cache_read, 0)

    @classmethod
    def from_poll(cls, d: Any) -> TokenCounts:
        """Poll-Form (``tokens`` eines Backend-Ergebnisses) → TokenCounts; malformiert → Nullen."""
        if not isinstance(d, Mapping):
            return cls()
        return cls(
            as_count(d.get("input")),
            as_count(d.get("output")),
            as_count(d.get("cache_read")),
            as_count(d.get("cache_write")),
        )

    @classmethod
    def from_usage(cls, u: Any) -> TokenCounts:
        """Step-Usage-Form → TokenCounts; fehlende/malformierte Felder → 0."""
        if not isinstance(u, Mapping):
            return cls()
        return cls(
            as_count(u.get("prompt_tokens")),
            as_count(u.get("completion_tokens")),
            as_count(u.get("cache_read_tokens")),
            as_count(u.get("cache_write_tokens")),
        )

    def as_poll(self) -> dict[str, int]:
        """Poll-Form für ``AgentBackend.poll``."""
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }

    def usage_fields(self) -> dict[str, int]:
        """Token-Felder der Step-Usage (``total_tokens`` = input + output)."""
        return {
            "prompt_tokens": self.input,
            "completion_tokens": self.output,
            "total_tokens": self.input + self.output,
            "cache_read_tokens": self.cache_read,
            "cache_write_tokens": self.cache_write,
        }

    def summary(self) -> str:
        """Log-Zeile, z. B. ``1.30M in (1.28M cached) / 10.1k out tokens``."""
        cached = f" ({compact(self.cache_read)} cached)" if self.cache_read else ""
        return f"{compact(self.input)} in{cached} / {compact(self.output)} out tokens"
