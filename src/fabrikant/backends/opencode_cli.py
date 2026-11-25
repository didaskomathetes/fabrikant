"""opencode as a one-shot CLI in the agent sandbox (ADR 0054): `opencode run --standalone`.

With `FABRIKANT_SANDBOX=1` the opencode adapter does not talk to a shared `opencode serve` on the host
but starts its own opencode instance per session in the hardened container (mechanics:
:class:`OneShotCliBackend`). The container is the boundary: no host mount, network only via the
egress proxy (ADR 0051), env only from the allowlist (ADR 0050).

Configuration: the model is called `<LLM connection>/<model>` (ADR 0052, e.g.
`llama-cpp/Qwen3.8-27B-GGUF`). A minimal opencode config is built from the connection
(:class:`OpenCodeConfig`): an OpenAI-compatible provider (endpoint without `/chat/completions`
as `baseURL`, `apiKey` only if the connection has a secret) plus the step's permission rules.
It goes into the container as `OPENCODE_CONFIG_CONTENT` — as `-e KEY` without a value in argv.
The endpoint host must be on the egress allowlist.

Isolation on top of the sandbox: no project config from the (untrusted) repo, no
Claude Code import, no model-catalog download, no auto-update, no sharing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .. import connections, egress
from ..tokens import TokenCounts
from ._oneshot import OneShotCliBackend
from .opencode import opencode_tokens

CONFIG_CONTENT_VAR = "OPENCODE_CONFIG_CONTENT"
# opencode switches in the sandbox: take nothing from the repo or HOME, download nothing.
ISOLATION_ENV = {
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
}
OPENAI_COMPATIBLE_PACKAGE = "@ai-sdk/openai-compatible"
CHAT_COMPLETIONS_SUFFIX = "/chat/completions"
# Without step rules: the container is the boundary — the agent may edit and use the shell.
DEFAULT_PERMISSIONS: dict[str, dict[str, str]] = {"edit": {"*": "allow"}, "bash": {"*": "allow"}}
# opencode 2.x: the base policy allows EVERYTHING (`*`/`*` allow) and config rules are appended
# afterwards (last match wins). So if the step names rules, all built-in permissions must be set to
# `*` deny, otherwise unnamed ones stay open (e.g. `bash` with only `edit` rules) and the agent
# bypasses the restriction through the shell.
BUILTIN_PERMISSIONS: tuple[str, ...] = ("edit", "bash", "read", "webfetch")
_FAILURE_TEXT_MAX = 300
WILDCARD, DENY = "*", "deny"


def provider_base_url(endpoint: str) -> str:
    """Endpoint of an `http` LLM connection → `baseURL` for opencode (without `/chat/completions`)."""
    url = endpoint.strip().rstrip("/")
    return url[: -len(CHAT_COMPLETIONS_SUFFIX)] if url.endswith(CHAT_COMPLETIONS_SUFFIX) else url


def endpoint_host(endpoint: str) -> str:
    """Hostname of the endpoint (for egress-allowlist hints)."""
    return urlsplit(endpoint).hostname or ""


def _host_allowed(host: str) -> bool:
    """Is the endpoint host on the egress allowlist (ADR 0051)? Subdomains count.
    Broken allowlist → False (fail-closed; the egress start reports the cause)."""
    try:
        allowed = egress.load_allowlist()
    except RuntimeError:
        return False
    return any(host == h or host.endswith("." + h) for h in allowed)


def permission_rules(rules: list | None) -> dict[str, dict[str, str]]:
    """Step `permissions` (1.x rules `{permission, pattern, action}`) → opencode config
    `{permission: {pattern: action}}`. Without rules: :data:`DEFAULT_PERMISSIONS`.

    Restrictive: opencode 2.x allows everything by base policy (`*`/`*` allow, last match
    wins) — so if the step names rules, all built-in permissions
    (:data:`BUILTIN_PERMISSIONS`) are set to `*` deny first, then the step rules on top (unless
    the step sets `*` itself). Otherwise unnamed ones would stay open — `edit: review.json allow`
    would also grant `bash` (and with it every other file)."""
    if not rules:
        return {k: dict(v) for k, v in DEFAULT_PERMISSIONS.items()}
    out: dict[str, dict[str, str]] = {perm: {WILDCARD: DENY} for perm in BUILTIN_PERMISSIONS}
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("permission") or not rule.get("action"):
            raise ValueError(f"opencode: Permission-Regel braucht permission + action: {rule!r}")
        out.setdefault(str(rule["permission"]), {})[str(rule.get("pattern") or "*")] = str(rule["action"])
    return out


@dataclass(frozen=True)
class OpenCodeConfig:
    """Minimal opencode config for a run: one provider from an LLM connection + permissions."""

    provider: str
    model: str
    base_url: str
    api_key: str = field(default="", repr=False)
    permissions: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_connection(cls, model_ref: str, permissions: list | None = None) -> OpenCodeConfig:
        """`<connection>/<model>` → config. If the model is missing, the connection's model applies.
        Raises :class:`connections.InvalidConnection` for an unknown/unsuitable connection."""
        name, _, model = model_ref.strip().partition("/")
        conn = connections.get_connection(name, connections.ConnectionKind.LLM)
        if conn is None:
            raise connections.InvalidConnection(
                f"opencode (Sandbox): LLM-Verbindung '{name}' unbekannt — Modell als <verbindung>/<modell> angeben"
            )
        if conn.driver != connections.DRIVER_HTTP or not conn.endpoint:
            raise connections.InvalidConnection(
                f"opencode (sandbox): connection '{name}' is not an OpenAI-compatible http endpoint ({conn.driver})"
            )
        if conn.secret_unreadable:
            raise connections.UndecryptableSecret(f"Connection '{name}': secret cannot be decrypted — enter it again")
        host = endpoint_host(conn.endpoint)
        if host and not _host_allowed(host):
            raise connections.InvalidConnection(
                f"opencode (sandbox): endpoint host '{host}' is not on the egress allowlist (ADR 0051) — "
                f"add the host via {egress.ALLOWLIST_VAR} or {egress.ALLOWLIST_FILE_VAR}"
            )
        return cls(
            provider=name,
            model=model or conn.model,
            base_url=provider_base_url(conn.endpoint),
            api_key=conn.secret,
            permissions=permission_rules(permissions),
        )

    @property
    def model_ref(self) -> str:
        return f"{self.provider}/{self.model}"

    def content(self) -> str:
        """JSON for `OPENCODE_CONFIG_CONTENT`."""
        options = {"baseURL": self.base_url} | ({"apiKey": self.api_key} if self.api_key else {})
        provider = {
            "npm": OPENAI_COMPATIBLE_PACKAGE,
            "name": self.provider,
            "options": options,
            "models": {self.model: {"name": self.model, "tool_call": True}},
        }
        return json.dumps(
            {
                "provider": {self.provider: provider},
                "model": self.model_ref,
                "permission": self.permissions,
                "autoupdate": False,
                "share": "disabled",
            }
        )


def parse_events(out: str) -> tuple[str, dict[str, int], float, list[str], str]:
    """JSONL von `opencode run --format json` → (Text, Tokens, Kosten, Tools, Fehler).

    Text = all `text` parts in order; tokens/cost summed over `step_finish`
    (cache tokens count as input, reasoning as output); error = the last `error` message."""
    texts: list[str] = []
    tools: list[str] = []
    tokens = TokenCounts()
    cost = 0.0
    error = ""
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        raw_part = event.get("part")
        part: dict = raw_part if isinstance(raw_part, dict) else {}
        if event.get("type") == "error":
            err = event.get("error")
            if isinstance(err, dict):
                # 2.2.5: `message` sits directly in the error object; older builds: `data.message`.
                error = str((err.get("data") or {}).get("message") or err.get("message") or err.get("name") or err)
            else:
                error = str(err)
        elif part.get("type") == "text" and part.get("text"):
            texts.append(str(part["text"]))
        elif part.get("type") == "tool" and part.get("tool"):
            tools.append(str(part["tool"]))
        elif part.get("type") == "step-finish":
            tokens += opencode_tokens(part.get("tokens"))
            cost += float(part.get("cost") or 0)
    return "\n".join(texts).strip(), tokens.as_poll(), cost, tools, error


class OpenCodeCliBackend(OneShotCliBackend):
    """Agent-Adapter: `opencode run --standalone` als One-Shot im Sandbox-Container."""

    name = "opencode"

    def __init__(self, endpoint: str = "", model: str = "", stale_after: int = 900, variant: str | None = None):
        super().__init__(endpoint, model.strip(), stale_after)
        self.variant = (variant or "").strip()
        self._config: OpenCodeConfig | None = None

    @classmethod
    def create(cls, step: dict, endpoint: str, model: str, stale_after: int) -> OpenCodeCliBackend:
        return cls(endpoint, model, stale_after=stale_after, variant=str(step.get("variant") or "") or None)

    def create_task(self, workdir: str, title: str, permissions: list | None = None) -> str:
        # Config per task: connection + the step's permission rules (fail early, before the container).
        self._config = OpenCodeConfig.from_connection(self.model, permissions)
        return super().create_task(workdir, title, permissions)

    # ------------------------------------------------------------------ Hooks
    def _cli(self) -> str:
        return "opencode"

    def health(self) -> bool:
        return True  # the binary lives in the sandbox image, not on the host

    def diagnose(self) -> str | None:
        return None

    def _stale_after(self) -> int | None:
        return None  # local models are slow; the limit is the step timeout

    def _env(self) -> dict:
        if self._config is None:
            raise RuntimeError("opencode: create_task vor send_prompt aufrufen")
        return {CONFIG_CONTENT_VAR: self._config.content(), **ISOLATION_ENV}

    def _build_cmd(self) -> list[str]:
        model = self._config.model_ref if self._config else self.model
        if self.variant:
            model = f"{model}#{self.variant}"
        return ["opencode", "run", "--standalone", "--format", "json", "--model", model]

    def _parse(self, out: str, err: str, returncode: int) -> dict:
        text, tokens, cost, tools, error = parse_events(out)
        base = {"tokens": tokens, "cost": cost, "tools": tools}
        if returncode == 0 and not error:
            return {**base, "status": "done", "text": text}
        reason = error or err.strip() or text or f"opencode: Exit {returncode}"
        return {**base, "status": "failed", "text": reason[-_FAILURE_TEXT_MAX:]}
