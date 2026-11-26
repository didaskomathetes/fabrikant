# ADR 0070: Rename to fabrikant

**Status:** Accepted (2026-11-24, merged to main). Amended 2026-11-24: the legacy compatibility layer (decision 2) was removed before the first public release; the project had no external users. Amended 2026-11-26: ADRs 0013–0024 use the new names (decision 6).
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
2. **No legacy compatibility.** The project had a single (private) user before the first public
   release, so no `FABRIK_*` / `fabrik.env` / `*.fabrik.yaml` / `fabrik.db` / `X-Fabrik-*` shims are shipped.
   (A compatibility layer was implemented first and removed again before release.)
3. **Clean break:** all surfaces in the table above, including the Python import path, CLI command,
   entry-point groups, Docker image names, systemd unit names, `refs/fabrik/relay/*` and harness
   directories. Existing private deployments update their unit files and env file name once.
4. **Execution** is a stacked, mechanically verifiable series (scripted rename + the full test
   suite, coverage gate, wheel smoke test), not a hand edit.
5. **Repository rename** on GitHub (`fabrik` → `fabrikant`) is a manual step by the owner; GitHub
   redirects the old URL.

6. **Existing ADRs are not renamed, except 0013–0024** (amended 2026-11-26; previously no ADR up to
   0069 was rewritten). ADRs 0013–0024 were translated to English and use the new names throughout,
   also where they describe events from before the rename. All other ADRs up to 0069 keep the old
   name `fabrik`; read it as `fabrikant`.

## Consequences

- (+) One name everywhere; `pip install fabrikant` works.
- (−) Third-party code importing `fabrik` breaks (none published yet).
- (−) Persisted artefacts that embed the old name (old run logs, `refs/fabrik/relay/*` in target
  repos) are not rewritten.
- (−) Large mechanical diff; reviewers should read the script, not every line.
