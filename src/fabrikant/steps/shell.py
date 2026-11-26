"""Step: shell (sh -c, cwd, env, timeout, variable substitution, secret redaction).

NEVER substitute untrusted values (e.g. LLM output, issue title/body) inline into `cmd`
— quotes/backticks/newlines in the value break the shell semantics
(silent no-op via `if` guard, command substitution). Pass them instead via `env:`
as an environment variable and reference them in the command as "$VAR":

    env:
      criteria: "{acceptance_criteria}"
    cmd: 'if [ -n "$criteria" ]; then …; fi'

The mixed pattern (inline placeholders in cmd that collide with `env:` keys of the same
step) is rejected fail-fast (ok: False) — a warning alone
would be ignorable, the substitution would run anyway.

Trust policy: trusted values (issue_number, run_id, config flags)
may stand inline in `cmd` — even if the same step sets `env:` for untrusted
values, as long as the placeholders do not collide with `env:` keys
(guard). Everything from LLM/issue context belongs in `env:` (ADR 0008).

Timeout: `timeout` (default 600 s). The command runs in its own process group
(`procs.run_group`): on expiry the whole process tree dies, not just `sh`; afterwards
`subprocess.TimeoutExpired` propagates (is caught by the engine).

Verdict: an output line `::fabrikant-verdict::<word>` (`runmodel.verdict_line`) reports the domain
result of the step (e.g. `fail` of an audit, the step itself stays green). It is removed from output and
summary and lands as `verdict` in the step record; the UI shows it as a badge.
With `done_marker: true` (opt-in, shell steps only), a line `::fabrikant-done::` (exit 0 only)
ends the run successfully after this step — the remaining steps are skipped (gate without work).
Without the key the line is ordinary output (not removed, no effect): output of tests, linters or
echoed PR text must not be able to skip later gates.
Lines `::fabrikant-warning::<text>` (stdout or stderr, also on exit ≠ 0) appear as ⚠ in the
run log and as `WARNING: <text>` at the end of the output — for problems that don't fail the step.

Failure class (ADR 0035): exit 126/127 → `environment`. Scripts can report their class
themselves (sysexits.h): exit 75 (EX_TEMPFAIL) → `transient`, 78 (EX_CONFIG) →
`environment`. Other exit codes cannot be interpreted by the shell; the spec names the
class for them via `failure_kind` (e.g. `outcome` for a test step), otherwise `unknown`.
"""

from __future__ import annotations

import subprocess

from ..context import _PLACEHOLDER_RE, _cap, _substitute, step_workdir
from ..failures import classify_exit_code, failed, is_signal_exit, parse_kind
from ..gitops import process_env
from ..i18n import t
from ..procs import run_group
from ..runmodel import FailureKind, split_done, split_verdict, split_warnings
from ..secrets import _redact
from ..spec_sources import resolve_script_paths


def run_shell(cmd: str, workdir: str, env: dict, timeout: int) -> subprocess.CompletedProcess:
    """`sh -c cmd` in the workdir (stdout/stderr captured separately); TimeoutExpired propagates."""
    return subprocess.run(["sh", "-c", cmd], cwd=workdir, env=env, capture_output=True, text=True, timeout=timeout)


def _step_shell(ctx: dict, step: dict, emit) -> dict:
    raw_cmd = step["cmd"]
    env_step = step.get("env") or {}
    # Guard (issue #62): fail fast only on real danger — an
    # inline placeholder in cmd that collides with an env: key of the same step.
    # This is the mixed pattern that led to shell injection/no-ops:
    # the placeholder is substituted from the (potentially untrusted) context
    # while the env: value is ignored. Fail fast instead of just a
    # WARN: a warning can be overlooked, a failed step cannot.
    # Trusted placeholders ({issue_number}, {run_id}, …) without
    # collision with env: keys stay allowed (trust policy, ADR 0008).
    # _PLACEHOLDER_RE is the shared syntax from context.py (== _substitute):
    # only real identifiers ({name}) trigger, literal braces ({123}, JSON)
    # do not — no false positive, and no placeholder is missed.
    if env_step:
        # Defensive str() cast: env keys can be non-str from external YAML parsers;
        # the collision check needs consistent types.
        clash = set(_PLACEHOLDER_RE.findall(raw_cmd)) & {str(k) for k in env_step}
        if clash:
            msg = t(
                "steps.shell.cmd_contains_inline_placeholders_that",
                v1=", ".join("{" + name + "}" for name in sorted(clash)),
            )
            emit(f"  ERROR: {msg}")
            return failed(msg, FailureKind.ENVIRONMENT, exit_code=1, output="", reason="placeholder-env-clash")
    cmd = resolve_script_paths(_substitute(raw_cmd, ctx), ctx.get("spec_path"))
    workdir = step_workdir(ctx, step)
    env = {**process_env(), **{k: _substitute(str(v), ctx) for k, v in env_step.items()}}
    timeout = int(step.get("timeout", 600))
    # Log the template (with {placeholders}), NOT the substituted command:
    # substituted values can contain secrets/untrusted text.
    emit(f"  $ {raw_cmd}  (cwd={workdir}, timeout={timeout}s)")
    p = run_group(["sh", "-c", cmd], cwd=workdir, env=env, timeout=timeout)
    # Secret redaction (defense in depth): stdout/stderr can contain secrets
    # (e.g. if the command echoes an env value). The step masks output/summary itself;
    # the engine additionally masks at record write (secrets._redact_record).
    # Verdict line (`::fabrikant-verdict::<word>`) is metadata, not output: out of output/summary.
    stdout, verdict = split_verdict(p.stdout or "")
    # `::fabrikant-done::` ends the run successfully — honoured only with the explicit opt-in
    # `done_marker: true`: otherwise any output (test/lint output, echoed PR text) could skip later gates.
    stdout, done = split_done(stdout) if step.get("done_marker") is True else (stdout, False)
    stdout, warnings = split_warnings(stdout)
    stderr, err_warnings = split_warnings(p.stderr or "")
    warnings = [_redact(w) for w in (*warnings, *err_warnings)]
    for w in warnings:
        emit(f"  ⚠ {w}")
    out = _redact(
        stdout
        + ("\n[stderr]\n" + stderr if stderr else "")
        + ("\n[warnings]\n" + "\n".join(f"WARNING: {w}" for w in warnings) if warnings else "")
    )
    # Tail: last lines of stdout, else stderr, else the warnings (a step whose only output is a
    # warning must not be summarised as "no output").
    tail_src = stdout.strip() or stderr.strip() or "\n".join(f"WARNING: {w}" for w in warnings)
    tail = _redact("\n".join(tail_src.splitlines()[-8:]))
    if p.returncode != 0:
        return failed(
            f"exit {p.returncode}: {tail.splitlines()[-1] if tail else 'no output'}",
            _exit_kind(p.returncode, step),
            exit_code=p.returncode,
            output=_cap(out),
        )
    res = {"ok": True, "exit_code": 0, "output": _cap(out), "summary": (tail.splitlines()[-1] if tail else "ok")}
    return res | ({"verdict": verdict} if verdict else {}) | ({"done": True} if done else {})


def _exit_kind(code: int, step: dict) -> FailureKind:
    """Class of an exit code ≠ 0: fixed codes (126/127/75/78) first, otherwise `step.failure_kind`
    (default unknown). Death by signal stays unknown: an OOM kill is not a red test result."""
    if is_signal_exit(code):
        return FailureKind.UNKNOWN
    kind = classify_exit_code(code)
    return kind if kind is not FailureKind.UNKNOWN else parse_kind(step.get("failure_kind"))
