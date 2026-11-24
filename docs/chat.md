# Chat, LLM providers and connections

The chat interface (UI, `fabrikant chat "..."`, `POST /api/chat`) builds, changes and debugs loops — via commands
or via the LLM copilot.

**Commands:** `help`, `list`, `show <project>`, `run <project> <loop> [note]` (German alias `lauf`; start a run),
`runs [project]`, `log <run_id>`, `approve <run_id>`, `reject <run_id>`, `pause|resume|disable <project> [loop]`,
`alert test [project]`, `apply <project>`

## LLM copilot

Everything that isn't a command goes to the **active** LLM provider (OpenAI-compatible). Providers are
configured in `fabrikant.env` and switched in the UI (select at the top right, `POST /api/llm`):

- **Named providers** (any number, e.g. Ollama + local llama.cpp): `FABRIKANT_LLM_PROVIDERS=ollama,llama-cpp` +
  `FABRIKANT_LLM_OLLAMA_URL`/`_MODEL`/`_KEY` and `FABRIKANT_LLM_LLAMA_CPP_URL`/`_MODEL`/`_KEY` (hyphens in the
  name → underscores in the env prefix).
- **CLI providers** (`_CLI` instead of `_URL`, auth = the CLI's login, no key): `FABRIKANT_LLM_CLAUDE_CLI=claude`
  / `FABRIKANT_LLM_COPILOT_CLI=copilot` + `_MODEL`. Runs without tools and without repo instructions (plain text
  answer); `temperature`/`max_tokens` are ignored.
- **Legacy** (one endpoint, becomes provider `default`): `FABRIKANT_LLM_URL`, `FABRIKANT_LLM_MODEL`, optionally
  `FABRIKANT_LLM_KEY`.

The active provider is persisted in `state/active_llm` (survives restarts); the default is the first configured
provider. `llm` steps can pin a provider via `provider:` and override the model via `model:`. The LLM sees the
current specs + the latest failed runs; if it proposes a spec change, it is validated and waits for
`apply <project>` (nothing is written without consent). While an LLM call is running, the chat shows animated
dots + a **cancel** button (`POST /api/chat/cancel`); the request ends immediately; an HTTP call finishes in the
background, a CLI is killed. The model dropdown sits in the chat's title bar.

## Connections (ADR 0052)

LLM providers and agent backends can be maintained as **named connections** in the UI (gear icon at the top
right, writing only as `admin`). Env configuration (`FABRIKANT_LLM_*`, `FABRIKANT_AGENT_*`) keeps applying and
appears read-only (`env`); a UI connection with the same name overrides it.

- An agent step selects via `connection: <name>`; without it, the connection marked as default applies (if its
  backend matches the step). Precedence: step fields > `connection:` > default > env.
- Secrets (API key, server password) are stored Fernet-encrypted in the `connections` table of the state DB
  (`FABRIKANT_DB_URL`) and are never returned via the API. Key: `FABRIKANT_SECRET_KEY`, otherwise
  `state/secret.key` (0600, generated automatically). Without the key, stored secrets are unusable — back up the
  key separately from the DB.

```yaml
steps:
  - {type: agent, name: changes, connection: hetzner-opencode, prompt_file: prompts/develop.md}
```
