"""Step: shell (sh -c, cwd, env, timeout, Variable-Substitution, Secret-Redaction).

Untrusted Werte (z. B. LLM-Output, Issue-Titel/-Body) NIE inline in `cmd`
substituieren — Quotes/Backticks/Newlines im Wert brechen die Shell-Semantik
(stilles No-op via `if`-Guard, Command-Substitution). Stattdessen über `env:`
als Umgebungsvariable übergeben und im Befehl als "$VAR" referenzieren:

    env:
      criteria: "{acceptance_criteria}"
    cmd: 'if [ -n "$criteria" ]; then …; fi'

Das Mischmuster (Inline-Placehalter in cmd, die mit `env:`-Keys desselben
Steps kollidieren) wird fail-fast abgelehnt (ok: False) — eine Warnung allein
wäre ignorierbar, die Substitution würde trotzdem laufen.

Trust-Policy: vertrauenswürdige Werte (issue_number, run_id, Config-Flags)
dürfen inline in `cmd` stehen — auch wenn derselbe Step `env:` für untrusted
Werte setzt, solange die Placehalter nicht mit `env:`-Keys kollidieren
(Guard). Alles aus LLM-/Issue-Kontext gehört in `env:` (ADR 0008).

Timeout: `timeout` (Default 600 s). Der Befehl läuft in eigener Prozessgruppe
(`procs.run_group`): bei Ablauf stirbt der ganze Prozessbaum, nicht nur `sh`; danach
propagiert `subprocess.TimeoutExpired` (wird von der Engine gefangen).

Verdikt: eine Ausgabezeile `::fabrik-verdict::<wort>` (`runmodel.verdict_line`) meldet das fachliche
Ergebnis des Steps (z. B. `fail` eines Audits, der Step selbst bleibt grün). Sie wird aus Output und
Summary entfernt und landet als `verdict` im Step-Record; die UI zeigt es als Badge.

Fehlerklasse (ADR 0035): Exit 126/127 → `environment`. Skripte können ihre Klasse
selbst melden (sysexits.h): Exit 75 (EX_TEMPFAIL) → `transient`, 78 (EX_CONFIG) →
`environment`. Andere Exit-Codes kann die Shell nicht deuten; die Spec benennt die
Klasse dafür über `failure_kind` (z. B. `outcome` für einen Test-Step), sonst `unknown`.
"""

from __future__ import annotations

import subprocess

from ..context import _PLACEHOLDER_RE, _cap, _substitute, step_workdir
from ..failures import classify_exit_code, failed, is_signal_exit, parse_kind
from ..gitops import process_env
from ..i18n import t
from ..procs import run_group
from ..runmodel import FailureKind, split_verdict
from ..secrets import _redact
from ..spec_sources import resolve_script_paths


def run_shell(cmd: str, workdir: str, env: dict, timeout: int) -> subprocess.CompletedProcess:
    """`sh -c cmd` im Workdir (stdout/stderr getrennt erfasst); TimeoutExpired propagiert."""
    return subprocess.run(["sh", "-c", cmd], cwd=workdir, env=env, capture_output=True, text=True, timeout=timeout)


def _step_shell(ctx: dict, step: dict, emit) -> dict:
    raw_cmd = step["cmd"]
    env_step = step.get("env") or {}
    # Guard (Issue #62): Fail-fast nur bei echter Gefährdung — ein
    # Inline-Placehalter in cmd, der mit einem env:-Key desselben Steps
    # kollidiert. Das ist das Mischmuster, das zu Shell-Injection/No-ops
    # führte: der Platzhalter wird aus dem (potenziell untrusted) Kontext
    # substituiert, während der env:-Wert ignoriert wird. Fail-fast statt nur
    # WARN: eine Warnung kann übersehen werden, ein fehlgeschlagener Step
    # nicht. Vertrauenswürdige Placehalter ({issue_number}, {run_id}, …) ohne
    # Kollision mit env:-Keys bleiben erlaubt (Trust-Policy, ADR 0008).
    # _PLACEHOLDER_RE ist die geteilte Syntax aus context.py (== _substitute):
    # nur echte Identifikatoren ({name}) triggern, Literal-Braces ({123}, JSON)
    # nicht — kein False-Positive, und kein Platzhalter wird verpasst.
    if env_step:
        # Defensive str()-Cast: env-Keys können aus externen YAML-Parsern
        # nicht-str sein; der Kollisions-Check braucht konsistente Typen.
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
    # Template (mit {placeholders}) loggen, NICHT den substituierten Befehl:
    # substituierte Werte können Secrets/untrusted Text enthalten.
    emit(f"  $ {raw_cmd}  (cwd={workdir}, timeout={timeout}s)")
    p = run_group(["sh", "-c", cmd], cwd=workdir, env=env, timeout=timeout)
    # Secret-Redaction (Defense-in-Depth): stdout/stderr können Secrets
    # enthalten (z. B. wenn der Befehl einen env-Wert echo-t). Der Step
    # maskiert output/summary selbst; die Engine maskiert zusätzlich beim
    # Record-Write (secrets._redact_record).
    # Verdikt-Zeile (`::fabrik-verdict::<wort>`) ist Metadatum, kein Output: raus aus Output/Summary.
    stdout, verdict = split_verdict(p.stdout or "")
    out = _redact(stdout + ("\n[stderr]\n" + p.stderr if p.stderr else ""))
    tail = _redact("\n".join((stdout or p.stderr or "").strip().splitlines()[-8:]))
    if p.returncode != 0:
        return failed(
            f"exit {p.returncode}: {tail.splitlines()[-1] if tail else 'kein Output'}",
            _exit_kind(p.returncode, step),
            exit_code=p.returncode,
            output=_cap(out),
        )
    res = {"ok": True, "exit_code": 0, "output": _cap(out), "summary": (tail.splitlines()[-1] if tail else "ok")}
    return res | ({"verdict": verdict} if verdict else {})


def _exit_kind(code: int, step: dict) -> FailureKind:
    """Klasse eines Exit-Codes ≠ 0: festgelegte Codes (126/127/75/78) zuerst, sonst `step.failure_kind`
    (Default unknown). Tod durch Signal bleibt unknown: ein OOM-Kill ist kein rotes Testergebnis."""
    if is_signal_exit(code):
        return FailureKind.UNKNOWN
    kind = classify_exit_code(code)
    return kind if kind is not FailureKind.UNKNOWN else parse_kind(step.get("failure_kind"))
