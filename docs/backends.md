# Agent-Backends

Agent-Steps (`backend:` im Step) nutzen ein pluggables Backend-Interface
(implizit, keine Base-Class): `name`, `health()`, `create_task(workdir, title, permissions) → session_id`,
`send_prompt(session_id, prompt, directory)`, `poll(session_id, directory) → {status, text, tokens, cost, tools}`,
`cancel(session_id, directory)`. Dispatch: `steps/agent.py` (`_step_agent_backend`) —
`step.backend` (oder `FABRIKANT_AGENT_BACKEND`) wählt, `model`/`endpoint` kommen aus
Step oder Env. Legacy-Wege (ohne `backend`) bleiben unberührt: `step.cmd`
(eigene CLI via Shell).

## Design notes (English)

- **Model routing.** Cost follows the difficulty: small models for
  triage, strong models for the build, and a *different* model with fresh
  context (ideally a different vendor) for the review — the Cold Critic
  (ADR 0003). Model choice is configuration, not architecture
  (ADR 0022).
- **Interface history.** The v1.1 design draft
  ([`archive/framework-v1-draft.md`](archive/framework-v1-draft.md), §2)
  proposed a frozen `AgentBackend` base class with
  `create_session`/`run`/`resume`/`health`/`budget`. That design was never
  frozen: the interface above is implicit (no base class) and shaped by
  what the backends actually support. There is no per-session `resume`;
  recovery is run-level (ADR 0036). See the [roadmap](roadmap.md) for the
  full reconciliation.
- **Cost & usage.** `poll()` reports `tokens: {input, output, cache_read,
  cache_write}` (`fabrikant.tokens.TokenCounts`). `input` is the whole prompt
  *including* the cache share. An agent re-reads its context every turn, so
  on long sessions most of `input` is cheap cache reads. A 1M-token run can
  cost about $0.50; check `cost_usd`, not the token count. What each backend
  can report:

  | Backend | input/output | cache_read | cache_write | USD |
  |---|---|---|---|---|
  | `claude_code` | ✓ | ✓ | ✓ | ✓ (`total_cost_usd`) |
  | `opencode` (API + CLI) | ✓ (reasoning counts as output) | ✓ | ✓ | ✓ |
  | `codex` | ✓ | ✓ (`cached_input_tokens`) | ✓ (`cache_write_input_tokens`) | – (credits/limits only in the rollout file, not in `--json`) |
  | `copilot` | – (only `premium_requests`) | – | – | – |
  | `llm` step, HTTP | ✓ | ✓ (`prompt_tokens_details.cached_tokens`) | – | – |

  Usage without `cost_usd` counts as `unpriced_steps`, and the summed cost
  is then a lower bound.
- **Billing (`usage.billing`).** `cost_usd` is computed from API list prices.
  It is only real money when the CLI runs on an API key. On a subscription
  login it is an API-equivalent estimate, and the run uses up plan limits or
  credits instead (`fabrikant.billing`):

  | Backend | `api` when | `subscription` when |
  |---|---|---|
  | `claude_code` | `ANTHROPIC_API_KEY` passed (wins in `-p` mode) | only `CLAUDE_CODE_OAUTH_TOKEN` |
  | `codex` | only `OPENAI_API_KEY` | only `CODEX_AUTH_JSON_B64` (ChatGPT login) |
  | `copilot` | – | always (premium requests) |
  | `opencode`, `llm` | – | – (provider-dependent: set it in the spec) |

  Anything else is `unknown`. A step can pin it with `billing: api |
  subscription | unknown`; for `opencode` and `llm` steps that is the only
  source. The UI shows subscription cost as `≈ $x API-equiv. (subscription
  limit)`. Fabrikant cannot see how much of a plan limit is left. Codex reports a
  reached workspace/member limit or depleted credits as an error, which is
  classified as `environment` (no announced reset).

One-Shot-CLI-Backends (`claude_code`, `codex`, `copilot`) leeren stdout/stderr ab dem
Spawn per Hintergrund-Thread (`_oneshot.py`, `_PipeDrain`): Eine erst nach Prozessende
gelesene Pipe läuft nach ~64 KiB voll — die CLI blockiert bzw. verwirft beim Shutdown den
Rest des Streams (copilot: exit 0, JSONL ohne `result`-Event; #153).

| Backend | Transport | Modell | Besonderes |
|---|---|---|---|
| `opencode` | HTTP (`opencode serve`, 1.19+) | `FABRIKANT_AGENT_MODEL` (provider/id) | `FABRIKANT_AGENT_ENDPOINT` nötig; Session-API, Polling; Containment via edit/bash-Permissions (relativ zur Workdir) |
| `claude_code` | CLI (`claude -p`, One-Shot) | `step.model` / `FABRIKANT_AGENT_MODEL` (sonnet/opus) | CLI muss im PATH sein; Prompt über **Stdin**; JSON-Output mit Usage/Cost; `endpoint` wird ignoriert |
| `codex` | CLI (`codex exec`, One-Shot) | `step.model` / `FABRIKANT_AGENT_MODEL` (optional) | CLI muss im PATH sein; Prompt über **stdin**; `--sandbox` Default `workspace-write`, Override `step.sandbox` / `FABRIKANT_CODEX_SANDBOX` |
| `copilot` | CLI (`copilot -p`, One-Shot) | `step.model` / `FABRIKANT_AGENT_MODEL` (optional) | CLI muss im PATH sein; Prompt über **stdin**; `--allow-all-tools` Default, Override `step.tools` / `FABRIKANT_COPILOT_TOOLS`; Auth via `COPILOT_GITHUB_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN` |

## opencode (P1)

Langlaufender HTTP-Server (`opencode serve`), persistente Sessions, Polling über
`GET /session/{id}/message`. Details: [`archive/spike-opencode.md`](archive/spike-opencode.md).

**Modelle** sind `provider/id` — der Provider muss in der opencode-Config
(`~/.config/opencode/opencode.jsonc`) des serve-Prozesses existieren.
Konfigurierte Provider:

- `llama` — LAN-llama.cpp (`llama.jabjabhome.duckdns.org`), z. B.
  `llama/Qwen3.8-27B-GGUF`.
- `hetzner` — Hetzner Inference (`inference.hetzner.com`), z. B.
  `hetzner/qwen3.8-27b` (→ Modell `Qwen3.8-27B`). API-Key liegt in
  `~/.config/opencode/secrets/hetzner-token` (Deployment-Voraussetzung).

## claude_code

One-Shot-Subprozess: jede Task = ein `claude -p` (Claude Code CLI, headless
Print-Mode). `send_prompt` startet den Prozess, der Prompt geht via **Stdin**
(kein ARG_MAX bei langen Prompts), das Ergebnis ist ein finales JSON auf Stdout
(`--output-format json`: `result`, `usage`, `total_cost_usd`, `subtype`, `is_error`).
`poll` meldet `running`, bis der Prozess endet, dann `done`/`failed`.

- **Model:** `--model <alias|name>` (z. B. `sonnet`, `claude-sonnet-4-5`); ein
  `anthropic/`-Prefix (opencode-Konvention) wird abgestreift.
- **Permissions:** Default `--permission-mode acceptEdits` + `--allowedTools Bash`
  (Agent editiert Dateien, darf Shell). Echte Isolation (non-root/Container/VM)
  ist Deployment-Voraussetzung, keine Config-Option.
- **`endpoint` wird ignoriert** (Interface-Kompatibilität) — nur `model` ist nötig.
- **Staleness:** nicht anwendbar wie bei opencode (kein inaktiver Server-State);
  der Step-Timeout ist der Backstop.

## codex (OpenAI Codex CLI)

One-Shot: `send_prompt` startet `codex exec --sandbox … [--model …]` im Workdir,
Prompt über stdin (keine ARG-Limits, keine Shell-Interpolation). `poll` wartet auf
den Exit — Exit 0 = `done` (stdout als Text), sonst `failed` (stderr). `cancel`
killed die Prozessgruppe (killpg) — keine Orphans.

- **Sandbox:** Default `workspace-write` (bubblewrap). In Gateway-/Service-Kontexten
  ohne bubblewrap: `FABRIKANT_CODEX_SANDBOX=danger-full-access` — Containment dann über
  User/VM/Container, nicht über die Sandbox.
- **Tokens ja, Kosten nein:** `codex exec --json` liefert Tokens je Turn (inkl.
  `cached_input_tokens`), aber keine USD-Kosten — der Step zählt als `unpriced`.
- **Auth:** Codex-CLI-Login (ChatGPT-Plan oder `FABRIKANT…`/`OPENAI_API_KEY` via
  `codex login`) — gehört in die Deployment-Umgebung, nicht in die Spec.

## copilot (GitHub Copilot CLI)

One-Shot: `send_prompt` startet `copilot --output-format json --no-ask-user
[--model …]` im Workdir, Prompt über **stdin** (`echo … | copilot` — keine
ARG-Limits, keine Shell-Interpolation; `-p` würde an `MAX_ARG_STRLEN` stoßen).
`poll` wartet auf den Exit — Exit 0 = `done` (finale Antwort aus dem JSONL
extrahiert), sonst `failed` (stderr). `cancel` killed die Prozessgruppe
(killpg) — keine Orphans.

- **Permissions:** Default `--allow-all-tools` (Agent editiert Dateien +
  Shell). Override `step.tools` / `FABRIKANT_COPILOT_TOOLS` (z. B. `shell,write`)
  → `--allow-tool=<list>`. Echte Isolation (non-root/Container/VM) ist
  Deployment-Voraussetzung, keine Config-Option.
- **Model:** `--model <name>` (z. B. `gpt-5.3-codex`, `claude-haiku-4.5`);
  optional — die CLI hat ein Default-Modell. Ein `anthropic/`-Prefix
  (opencode-Konvention) wird abgestreift.
- **Output:** `--output-format json` → JSONL (eine JSON-Zeile pro Event). Das
  Schema ist nicht vollständig dokumentiert, daher defensive Extraktion:
  `result`-Feld > letzter Assistant-Text > roher stdout (Fallback).
- **Keine Token-/Cost-Daten:** Copilot rechnet mit Premium Requests/AI-Credits,
  die CLI liefert kein Usage — die Step-Summary zeigt 0 tokens (wie codex).
- **Auth:** `COPILOT_GITHUB_TOKEN` > `GH_TOKEN` > `GITHUB_TOKEN` (fine-grained
  PAT mit "Copilot Requests") — gehört in die Deployment-Umgebung, nicht in
  die Spec. `COPILOT_AUTO_UPDATE=false` wird pro Prozess gesetzt.
- **Staleness:** wie codex — `stale_after` (Default 900s) als Backstop für
  hängende Prozesse.
