# ADR 0008: Shell steps with untrusted input — `env:` instead of inline substitution

> Originally written in German; translated to English 2026-11-19 (wording only, content unchanged).

**Status:** Accepted (2026-10-18) — amendment (guard semantics + compatibility) in draft, PR #72; amended 2026-11-03 (PR #347): trust default for `extra_context` sources

## Context

Shell steps (`type: shell`) replace `{placeholders}` in `cmd` via
`_substitute()` (variable substitution from the run context). Some of these
variables are **untrusted**: LLM output (e.g. triage `acceptance_criteria`),
issue titles/bodies.

If untrusted text is substituted **inline** into the command, quotes/
backticks/newlines/`$` sequences break the shell semantics:

- **Silent no-op:** a newline/quote in the value chops up an `if` condition →
  the guard becomes `false` → `exit 0` → the step reports "pass" although nothing
  happened.
- **Command substitution:** backticks (`` `…` ``) or `$(…)` in the value are
  **executed** by the shell → shell injection.

Regression: issue #62 (triage loop `add-acceptance` substituted the
LLM `acceptance_criteria` inline).

## Decision

**Untrusted values belong exclusively in `env:`, not in `cmd`.**

1. **`env:` mechanism:** untrusted values are passed as an environment variable
   and referenced in the command as `"$VAR"`. The shell treats the
   value as a single, inert argument — no interpretation of
   quotes/backticks/`$`.

   ```yaml
   env:
     criteria: "{acceptance_criteria}"
   cmd: 'if [ -n "$criteria" ]; then …; fi'
   ```

2. **Fail-fast guard against the mixed pattern:** a step in which an
   inline placeholder in `cmd` **collides with an `env:` key of the same step**
   is **rejected** (`ok: False`, `exit_code: 1`) — not merely
   warned about. A warning can be ignored and the inline substitution would
   run anyway; a failed step cannot. The collision is the
   actual danger: the placeholder is substituted from the (potentially untrusted)
   context while the `env:` value is ignored.
   Trusted placeholders (`{issue_number}`, `{run_id}`, …) without a
   collision with `env:` keys remain allowed — even in steps that use `env:` for
   untrusted values. Placeholder detection uses a
   regex (`\{[A-Za-z_][A-Za-z0-9_]*\}`, same syntax as `_substitute`),
   so literal braces (e.g. JSON `{"a": 1}`) trigger **no** false positive.

3. **Log masking:** the step logs the **command template** (with
   `{placeholders}`), not the substituted command — substituted values
   may contain secrets/untrusted text and must not end up in the run log.

4. **Trust default for `extra_context` sources** (PR #347): every source is
   untrusted unless it sets `trusted: true`. That is only intended for repo-owned
   files and tool output; issue text and PR comments remain untrusted.
   Untrusted content goes through `untrusted_blocks`. In there `<untrusted`/`<context` tags
   are neutralized so that text cannot close a block or fake a trusted block.

## Consequences

- Deterministic security behavior: the unsafe mixed pattern fails
  fast instead of silently being a no-op or injecting
- No secret leak in the run log (template instead of substituted command)
- Untrusted text is preserved byte for byte (no silent no-op)
- Regex detection = same placeholder syntax as `_substitute` (consistent,
  no false positives on literal braces)
- Existing shell steps with inline untrusted values must be migrated to `env:`
  (otherwise fail-fast)
- `env:` values are resolved via `_substitute` — trusted
  config values (e.g. `{issue_number}`) may still appear inline in `cmd`
  as long as they do not collide with `env:` keys of the same step

## Compatibility & migration

The placeholder regex is deliberately **strict**: `\{[A-Za-z_][A-Za-z0-9_]*\}`
requires a real identifier (first character letter/`_`). That is a
**breaking change** compared to the earlier, looser syntax:

- **Numeric/other placeholders** such as `{123}` are **no longer
  substituted** — they stay literal in the command.
- **Migration:** affected steps (if any) must change the placeholders to
  identifier form (e.g. `{123}` → `{num123}`, or name the
  context key accordingly). In the current project (`projects/*.yaml`)
  there are no numeric placeholders — the migration is a no-op there.

The guard itself is **relaxed** (not a breaking change): steps with `env:`
and inline placeholders that do **not** collide with `env:` keys still
run — previously they were rejected across the board.

## Alternatives (rejected)

- **Warn-only:** ignorable; unsafe cases run anyway → inconsistent
  security level.
- **CI lint rule only:** catches only statically detectable cases, no
  runtime protection; less direct than a fail-fast step.
- **Shell-quoting the substituted values:** does not prevent the pattern,
  is error-prone (getting the quoting right is exactly the problem) and
  changes the semantics of legitimate inline use.
