# Agent backends

Agent steps (`backend:` in the step) use a pluggable backend interface (implicit, no base class): `name`,
`health()`, `create_task(workdir, title, permissions) → session_id`, `send_prompt(session_id, prompt, directory)`,
`poll(session_id, directory) → {status, text, tokens, cost, tools}`, `cancel(session_id, directory)`. Dispatch:
`steps/agent.py` (`_step_agent_backend`) — `step.backend` (or `FABRIKANT_AGENT_BACKEND`) selects the backend;
`model`/`endpoint` come from the step or the environment. Legacy paths (without `backend`) are unaffected:
`step.cmd` (your own CLI via shell).

## Design notes

- **Model routing.** Cost follows the difficulty: small models for
  triage, strong models for the build, and a *different* model with fresh
  context (ideally a different vendor) for the review — the Cold Critic
  (ADR 0003). Model choice is configuration, not architecture
  (ADR 0022).
- **Interface history.** An early design draft
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

One-shot CLI backends (`claude_code`, `codex`, `copilot`) drain stdout/stderr from spawn on via a background
thread (`_oneshot.py`, `_PipeDrain`): a pipe that is only read after the process ends fills up at ~64 KiB, and the
CLI then blocks or drops the rest of the stream on shutdown (copilot: exit 0, JSONL without a `result` event;
#153).

| Backend | Transport | Model | Notes |
|---|---|---|---|
| `opencode` | HTTP (`opencode serve`, 1.19+) | `FABRIKANT_AGENT_MODEL` (provider/id) | needs `FABRIKANT_AGENT_ENDPOINT`; session API, polling; containment via edit/bash permissions (relative to the workdir) |
| `claude_code` | CLI (`claude -p`, one-shot) | `step.model` / `FABRIKANT_AGENT_MODEL` (sonnet/opus) | CLI must be on PATH; prompt via **stdin**; JSON output with usage/cost; `endpoint` is ignored |
| `codex` | CLI (`codex exec`, one-shot) | `step.model` / `FABRIKANT_AGENT_MODEL` (optional) | CLI must be on PATH; prompt via **stdin**; `--sandbox` defaults to `workspace-write`, override with `step.sandbox` / `FABRIKANT_CODEX_SANDBOX` |
| `copilot` | CLI (`copilot -p`, one-shot) | `step.model` / `FABRIKANT_AGENT_MODEL` (optional) | CLI must be on PATH; prompt via **stdin**; `--allow-all-tools` by default, override with `step.tools` / `FABRIKANT_COPILOT_TOOLS`; auth via `COPILOT_GITHUB_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN` |

## opencode (P1)

Long-running HTTP server (`opencode serve`), persistent sessions, polling via `GET /session/{id}/message`.

**Models** are `provider/id` — the provider must exist in the opencode config (`~/.config/opencode/opencode.jsonc`)
of the serve process. Any OpenAI-compatible endpoint works, e.g. a local llama.cpp server (`llama/<model>`) or a
hosted inference provider (`<provider>/<model>`). API keys belong in a file outside the repo (e.g.
`~/.config/opencode/secrets/<provider>-token`), never in the spec.

## claude_code

One-shot subprocess: each task = one `claude -p` (Claude Code CLI, headless print mode). `send_prompt` starts the
process and passes the prompt via **stdin** (no ARG_MAX issue with long prompts); the result is a final JSON on
stdout (`--output-format json`: `result`, `usage`, `total_cost_usd`, `subtype`, `is_error`). `poll` reports
`running` until the process ends, then `done`/`failed`.

- **Model:** `--model <alias|name>` (e.g. `sonnet`, `claude-sonnet-4-5`); an `anthropic/` prefix (opencode
  convention) is stripped.
- **Permissions:** default `--permission-mode acceptEdits` + `--allowedTools Bash` (the agent edits files and may
  use the shell). Real isolation (non-root/container/VM) is a deployment prerequisite, not a config option.
- **Tool restriction per step:** both keys take a list or a comma-separated string.
  - `tools` is the hard limit: only the named built-ins exist in the session (`--tools`). It does not cover MCP
    tools.
  - `allowed_tools` are pre-approval rules (`--allowedTools`) and switch to `--permission-mode dontAsk`: a call
    that would otherwise prompt is denied. This is not an allowlist. Calls that need no approval (file reads in
    the working directory, read-only Bash commands) still run, and so do calls approved by `permissions.allow`
    rules in the user, project or local `settings.json` of the machine running the agent. To really keep a tool
    out, leave it out of `tools`.
  - An empty value is rejected by validation; omit the key for the default.

  Example for a reviewer that may read the repo and write only its payload (`Write` exists but is not
  pre-approved, so it is denied unless a settings file allows it):

  ```yaml
  - type: agent
    backend: claude_code
    tools: [Read, Grep, Glob, Edit, Write]
    allowed_tools: [Read, Grep, Glob, "Edit(/.fabrikant-session/review/review.json)"]
  ```
- **`endpoint` is ignored** (interface compatibility) — only `model` is needed.
- **Staleness:** doesn't apply as with opencode (no idle server state); the step timeout is the backstop.

## codex (OpenAI Codex CLI)

One-shot: `send_prompt` starts `codex exec --sandbox … [--model …]` in the workdir, prompt via stdin (no argument
limits, no shell interpolation). `poll` waits for the exit — exit 0 = `done` (stdout as text), otherwise `failed`
(stderr). `cancel` kills the process group (killpg) — no orphans.

- **Sandbox:** default `workspace-write` (bubblewrap). In gateway/service contexts without bubblewrap:
  `FABRIKANT_CODEX_SANDBOX=danger-full-access` — containment then comes from the user/VM/container, not the
  sandbox.
- **Tokens yes, cost no:** `codex exec --json` reports tokens per turn (incl. `cached_input_tokens`) but no USD
  cost — the step counts as `unpriced`.
- **Auth:** Codex CLI login (ChatGPT plan or `OPENAI_API_KEY` via `codex login`) — belongs in the deployment
  environment, not the spec.

## copilot (GitHub Copilot CLI)

One-shot: `send_prompt` starts `copilot --output-format json --no-ask-user [--model …]` in the workdir, prompt via
**stdin** (`echo … | copilot` — no argument limits, no shell interpolation; `-p` would hit `MAX_ARG_STRLEN`).
`poll` waits for the exit — exit 0 = `done` (final answer extracted from the JSONL), otherwise `failed` (stderr).
`cancel` kills the process group (killpg) — no orphans.

- **Permissions:** default `--allow-all-tools` (the agent edits files + shell). Override with `step.tools` /
  `FABRIKANT_COPILOT_TOOLS` (e.g. `shell,write`) → `--allow-tool=<list>`. Real isolation (non-root/container/VM)
  is a deployment prerequisite, not a config option.
- **Model:** `--model <name>` (e.g. `gpt-5.3-codex`, `claude-haiku-4.5`); optional — the CLI has a default model.
  An `anthropic/` prefix (opencode convention) is stripped.
- **Output:** `--output-format json` → JSONL (one JSON line per event). The schema isn't fully documented, so
  extraction is defensive: `result` field > last assistant text > raw stdout (fallback).
- **No token/cost data:** Copilot bills in premium requests/AI credits and the CLI reports no usage — the step
  summary shows 0 tokens (like codex).
- **Auth:** `COPILOT_GITHUB_TOKEN` > `GH_TOKEN` > `GITHUB_TOKEN` (fine-grained PAT with "Copilot Requests") —
  belongs in the deployment environment, not the spec. `COPILOT_AUTO_UPDATE=false` is set per process.
- **Staleness:** like codex — `stale_after` (default 900 s) is the backstop for hung processes.

## Security layers for agent processes

Can be enabled individually; see the ADRs.

- **Agent env allowlist (ADR 0050):** agent subprocesses don't inherit `os.environ` — only the allowlist
  (`agentenv.py`, extendable via `FABRIKANT_AGENT_ENV_EXTRA`) plus variables passed explicitly per step. For the
  shared `opencode serve` process: `fabrikant agent-env` prints the scrubbed env (systemd `EnvironmentFile` /
  Docker `--env-file`).
- **Egress default-deny (ADR 0051):** agent containers attach to `fabrikant-sandbox` (no direct internet access);
  all egress goes through a Tinyproxy with a domain allowlist (`fabrikant egress start`; allowlist: builtins +
  optional `FABRIKANT_EGRESS_ALLOWLIST_FILE`/`FABRIKANT_EGRESS_ALLOWLIST`, validated; see
  `deploy/egress-allowlist.txt`). Denied requests in the proxy log are an injection signal. Known limit: the
  shared `opencode serve` process runs unsandboxed on the host with full network access and isn't covered by the
  proxy (only agents in `fabrikant-sandbox`).
- **Per-run agent sandbox (ADR 0054, under revision):** with `FABRIKANT_SANDBOX=1`, every one-shot CLI agent run
  executes in a hardened Docker container (`--cap-drop ALL`, no-new-privileges, resource caps, read-only root fs,
  egress only via the ADR 0051 proxy). The worktree is not mounted: the state goes in as a git bundle, the result
  comes back as a bundle; fabrikant checks it and applies the diff to the worktree. The image needs the agent CLI
  and git; template with claude-code, codex, copilot, gh, Python/uv:
  `docker build -t fabrikant/agent-sandbox:latest deploy/agent-sandbox` (versions via `--build-arg`, base images
  pinned by digest; fabrikant starts it with `--pull never`). A GitHub token only enters the container with
  `github_identity`; without it all token variables are empty (also for projects with `github_env_file`).

## opencode setup and behavior

Host: `opencode serve --port 8945` + `fabrikant.env`: `FABRIKANT_AGENT_ENDPOINT`, `FABRIKANT_AGENT_MODEL` (format
`provider/model`); 2.x additionally `FABRIKANT_AGENT_PASSWORD`. With `FABRIKANT_SANDBOX=1`: no `serve` — one
`opencode run --standalone` per session inside the sandbox container, model as `<LLM connection>/<model>` (e.g.
`llama-cpp/Qwen3.8-27B-GGUF`); the provider config is derived from the connection (http endpoint, secret only if
set), whose host must be on the egress allowlist. Step `permissions` are restrictive: listed patterns apply, all
others of the same permission are `deny`. Legacy agent steps can instead use `cmd:` with any agent CLI (e.g.
`claude -p {prompt_file}`).

- Supports opencode serve **1.x and 2.x** (ADR 0005) — the server version is detected automatically
  (`/api/info` vs. `/api/health`). 2.x requires HTTP basic auth: `OPENCODE_SERVER_PASSWORD` on the server +
  `FABRIKANT_AGENT_PASSWORD` in `fabrikant.env` (same value).
- One session per step (workdir via `?directory=` (1.x) or `location` (2.x) → work happens in the step workdir,
  one serve process serves many projects).
- Prompt + polling of the message API (no SSE needed); done/failed detection via `finish` + substance check
  (completed tool or final text) — empty/wrong messages (e.g. LLM 500) → step `fail`, no 30-minute hang.
- Token/cost accounting per run (from the message API).
- Health gate before dispatch; timeout → `abort`/`interrupt`; an exception mid-run aborts the session (no
  orphaning).
- Robust against transient backend/transport errors (ADR 0012): if `poll()`/`transcript()` throws (e.g.
  `opencode serve` down/restart), the step keeps polling with backoff — **within the step timeout** — instead of
  killing the run. A persistent error until the timeout → `fail` with the backend cause.
- Containment: file tools (`edit`) are confined to the session workdir (`external_directory` = ask → deny when
  headless); `bash "*"` is deliberately broad — real isolation (non-root user, container, VM) is a **deployment
  prerequisite**, not a config option. Override: `step.permissions`.
- Security: agent steps are T2+ — the conductor trusts only the deterministic step outcome, not agent prose;
  untrusted inputs (issue bodies) are data in the prompt.
