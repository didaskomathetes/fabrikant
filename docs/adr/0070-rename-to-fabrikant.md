# ADR 0070: Rename to fabrikant

**Status:** Accepted (2026-11-24, merged to main)
**Affects:** ADR 0069 (distribution and plugin contract), ADR 0047 (spec sources), ADR 0063 (provider registry).

## Context

`fabrik` is taken on PyPI, so `pip install fabrik` is impossible (ADR 0069). The project wants one
consistent name across distribution, import package, CLI, environment, entry points and docs.
The replacement is **fabrikant** (German/Dutch "maker, manufacturer": the one who runs the fabrik).
Nothing has been published to PyPI yet, so the plugin contract can still change for free.

## Decision

1. **Everything is renamed** (no split between distribution and import name):

   | Surface | Before | After |
   |---|---|---|
   | PyPI distribution, GHCR image | `fabrik` | `fabrikant` |
   | Import package | `fabrik` (`src/fabrik`) | `fabrikant` (`src/fabrikant`) |
   | CLI / MCP script | `fabrik`, `fabrik-mcp` | `fabrikant`, `fabrikant-mcp` |
   | Environment variables | `FABRIK_*` | `FABRIKANT_*` |
   | Entry-point groups | `fabrik.steps`, `fabrik.hosting_provider` | `fabrikant.steps`, `fabrikant.hosting_provider` |
   | Env file, spec suffix, target spec | `fabrik.env`, `*.fabrik.yaml`, `.fabrik/fabrik.yaml` | `fabrikant.env`, `*.fabrikant.yaml`, `.fabrikant/fabrikant.yaml` |
   | Session/harness dirs, markers, refs | `.fabrik-session`, `::fabrik-verdict::`, `refs/fabrik/…` | `.fabrikant-session`, `::fabrikant-verdict::`, `refs/fabrikant/…` |
   | Webhook headers | `X-Fabrik-Signature`, … | `X-Fabrikant-Signature`, … |
   | Docs, UI, prompts | fabrik / Fabrik | fabrikant / Fabrikant |

   German prose that uses *Fabrik* as the common noun ("Software-Fabrik") is **not** renamed.
2. **Legacy compatibility where data or deployments already exist** (read old, write new):
   - `FABRIK_*` environment variables are honoured when the `FABRIKANT_*` twin is unset (one shim at
     config load, logs a deprecation warning once).
   - Spec discovery accepts `*.fabrik.yaml/json` and `.fabrik/fabrik.yaml` besides the new names.
   - An existing `state/fabrik.db` is used when `state/fabrikant.db` does not exist.
   - Webhook receivers accept `X-Fabrik-*` headers besides `X-Fabrikant-*`.
   - `fabrik.env` is loaded when no `fabrikant.env` exists.
3. **Clean break (no shim):** Python import path, CLI command, entry-point groups, Docker image
   names, systemd unit names, `refs/fabrik/relay/*` and harness directories. Entry-point groups and
   import path have no external users yet; deployments update unit files once (see
   `docs/migration-fabrikant.md`).
4. **Execution** is a stacked, mechanically verifiable series (scripted rename + the full test
   suite, coverage gate, wheel smoke test), not a hand edit.
5. **Repository rename** on GitHub (`fabrik` → `fabrikant`) is a manual step by the owner; GitHub
   redirects the old URL.

6. **ADRs stay historical.** ADR 0069 and earlier keep the old name `fabrik` (decision records are not
   rewritten); read them with `fabrik` = `fabrikant`.

## Consequences

- (+) One name everywhere; `pip install fabrikant` works.
- (+) Existing homelab installs keep running after upgrade (env, specs, state db), with warnings.
- (−) Third-party code importing `fabrik` breaks (none published yet).
- (−) Persisted artefacts that embed the old name (old run logs, `refs/fabrik/relay/*` in target
  repos) are not rewritten.
- (−) Large mechanical diff; reviewers should read the script and the compat shims, not every line.
