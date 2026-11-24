"""opencode als One-Shot-CLI in der Agent-Sandbox (ADR 0054): `opencode run --standalone`.

Mit `FABRIKANT_SANDBOX=1` spricht der opencode-Adapter keinen geteilten `opencode serve` auf dem Host
an, sondern startet pro Session eine eigene opencode-Instanz im gehärteten Container (Mechanik:
:class:`OneShotCliBackend`). Der Container ist die Grenze: kein Host-Mount, Netz nur über den
Egress-Proxy (ADR 0051), Env nur aus der Allowlist (ADR 0050).

Konfiguration: Das Modell heißt `<LLM-Verbindung>/<modell>` (ADR 0052, z. B.
`llama-cpp/Qwen3.8-27B-GGUF`). Aus der Verbindung entsteht eine minimale opencode-Config
(:class:`OpenCodeConfig`): ein OpenAI-kompatibler Provider (Endpoint ohne `/chat/completions`
als `baseURL`, `apiKey` nur, wenn die Verbindung ein Secret hat) plus die Permission-Regeln des
Steps. Sie geht als `OPENCODE_CONFIG_CONTENT` in den Container — als `-e KEY` ohne Wert im argv.
Der Host des Endpoints muss auf der Egress-Allowlist stehen.

Isolation zusätzlich zur Sandbox: keine Projekt-Config aus dem (untrusted) Repo, keine
Claude-Code-Übernahme, kein Model-Katalog-Download, kein Auto-Update, kein Sharing.
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
# opencode-Schalter in der Sandbox: nichts aus dem Repo oder HOME übernehmen, nichts nachladen.
ISOLATION_ENV = {
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
}
OPENAI_COMPATIBLE_PACKAGE = "@ai-sdk/openai-compatible"
CHAT_COMPLETIONS_SUFFIX = "/chat/completions"
# Ohne Step-Regeln: der Container ist die Grenze — Agent darf editieren und die Shell nutzen.
DEFAULT_PERMISSIONS: dict[str, dict[str, str]] = {"edit": {"*": "allow"}, "bash": {"*": "allow"}}
# opencode 2.x: die Basis-Policy erlaubt ALLES (`*`/`*` allow) und Config-Regeln werden danach
# angehängt (letzter Treffer gewinnt). Nennt der Step also Regeln, müssen alle eingebauten
# Permissions auf `*` deny, sonst bleibt Ungenanntes frei (z. B. `bash` bei nur `edit`-Regeln)
# und der Agent umgeht die Einschränkung über die Shell.
BUILTIN_PERMISSIONS: tuple[str, ...] = ("edit", "bash", "read", "webfetch")
_FAILURE_TEXT_MAX = 300
WILDCARD, DENY = "*", "deny"


def provider_base_url(endpoint: str) -> str:
    """Endpoint einer `http`-LLM-Verbindung → `baseURL` für opencode (ohne `/chat/completions`)."""
    url = endpoint.strip().rstrip("/")
    return url[: -len(CHAT_COMPLETIONS_SUFFIX)] if url.endswith(CHAT_COMPLETIONS_SUFFIX) else url


def endpoint_host(endpoint: str) -> str:
    """Hostname des Endpoints (für Hinweise zur Egress-Allowlist)."""
    return urlsplit(endpoint).hostname or ""


def _host_allowed(host: str) -> bool:
    """Endpoint-Host auf der Egress-Allowlist (ADR 0051)? Subdomänen zählen mit.
    Kaputte Allowlist → False (fail-closed; der egress-Start meldet die Ursache)."""
    try:
        allowed = egress.load_allowlist()
    except RuntimeError:
        return False
    return any(host == h or host.endswith("." + h) for h in allowed)


def permission_rules(rules: list | None) -> dict[str, dict[str, str]]:
    """Step-`permissions` (1.x-Regeln `{permission, pattern, action}`) → opencode-Config
    `{permission: {pattern: action}}`. Ohne Regeln: :data:`DEFAULT_PERMISSIONS`.

    Restriktiv: opencode 2.x erlaubt per Basis-Policy alles (`*`/`*` allow, letzter Treffer
    gewinnt) — nennt der Step also Regeln, werden alle eingebauten Permissions
    (:data:`BUILTIN_PERMISSIONS`) auf `*` deny gesetzt, dann die Step-Regeln darüber (sofern
    der Step `*` nicht selbst setzt). Sonst bliebe Ungenanntes frei — `edit: review.json allow`
    würde `bash` (und damit jede andere Datei) mit freigeben."""
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
    """Minimale opencode-Config für einen Lauf: ein Provider aus einer LLM-Verbindung + Permissions."""

    provider: str
    model: str
    base_url: str
    api_key: str = field(default="", repr=False)
    permissions: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_connection(cls, model_ref: str, permissions: list | None = None) -> OpenCodeConfig:
        """`<verbindung>/<modell>` → Config. Fehlt das Modell, gilt das der Verbindung.
        Wirft :class:`connections.InvalidConnection` bei unbekannter/ungeeigneter Verbindung."""
        name, _, model = model_ref.strip().partition("/")
        conn = connections.get_connection(name, connections.ConnectionKind.LLM)
        if conn is None:
            raise connections.InvalidConnection(
                f"opencode (Sandbox): LLM-Verbindung '{name}' unbekannt — Modell als <verbindung>/<modell> angeben"
            )
        if conn.driver != connections.DRIVER_HTTP or not conn.endpoint:
            raise connections.InvalidConnection(
                f"opencode (Sandbox): Verbindung '{name}' ist kein OpenAI-kompatibler http-Endpoint ({conn.driver})"
            )
        if conn.secret_unreadable:
            raise connections.UndecryptableSecret(f"Verbindung '{name}': Secret nicht entschlüsselbar — neu eintragen")
        host = endpoint_host(conn.endpoint)
        if host and not _host_allowed(host):
            raise connections.InvalidConnection(
                f"opencode (Sandbox): Endpoint-Host '{host}' steht nicht auf der Egress-Allowlist (ADR 0051) — "
                f"Host per {egress.ALLOWLIST_VAR} oder {egress.ALLOWLIST_FILE_VAR} ergänzen"
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
        """JSON für `OPENCODE_CONFIG_CONTENT`."""
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

    Text = alle `text`-Parts in Reihenfolge; Tokens/Kosten summiert über `step_finish`
    (Cache-Tokens zählen zum Input, Reasoning zum Output); Fehler = letzte `error`-Meldung."""
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
                # 2.2.5: `message` liegt direkt im Error-Objekt; ältere Builds: `data.message`.
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
        # Config pro Task: Verbindung + Permission-Regeln des Steps (Fehler früh, vor dem Container).
        self._config = OpenCodeConfig.from_connection(self.model, permissions)
        return super().create_task(workdir, title, permissions)

    # ------------------------------------------------------------------ Hooks
    def _cli(self) -> str:
        return "opencode"

    def health(self) -> bool:
        return True  # das Binary liegt im Sandbox-Image, nicht auf dem Host

    def diagnose(self) -> str | None:
        return None

    def _stale_after(self) -> int | None:
        return None  # lokale Modelle brauchen lange; die Grenze ist das Step-Timeout

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
