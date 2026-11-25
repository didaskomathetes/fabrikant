"""Step: shell — Substitution, env-Mechanismus, Failure, Timeout.

Wichtigster Teil: untrusted Text (LLM-Output) gehört über `env:` in Shell-Steps,
nicht als Inline-Substitution in den Befehl (Shell-Injection: Quotes/Backticks/
Newlines brechen die Shell-Semantik → stilles No-op oder Command-Substitution).
Regression: Issue #62 (add-acceptance-Step im Triage-Loop).
"""

import os
import subprocess

import pytest

from fabrikant import secrets
from fabrikant.steps import _step_shell

NOOP = lambda m: None  # noqa: E731


def _ctx(tmp_path, **extra):
    return {"workdir": str(tmp_path), **extra}


# ---------------------------------------------------------------- Grundlagen
def test_shell_pass(tmp_path):
    res = _step_shell(_ctx(tmp_path), {"type": "shell", "cmd": "echo hello"}, NOOP)
    assert res["ok"] is True
    assert res["exit_code"] == 0
    assert "hello" in res["output"]


def test_shell_fail_exit_code(tmp_path):
    res = _step_shell(_ctx(tmp_path), {"type": "shell", "cmd": "echo oops >&2; exit 3"}, NOOP)
    assert res["ok"] is False
    assert res["exit_code"] == 3
    assert "oops" in res["output"]


def test_shell_inline_substitution(tmp_path):
    res = _step_shell(_ctx(tmp_path, issue_number="62"), {"type": "shell", "cmd": "echo issue {issue_number}"}, NOOP)
    assert res["ok"] is True
    assert "issue 62" in res["output"]


def test_shell_unknown_var_stays_literal(tmp_path):
    res = _step_shell(_ctx(tmp_path), {"type": "shell", "cmd": "echo {keine_var}"}, NOOP)
    assert res["ok"] is True
    assert "{keine_var}" in res["output"]


def test_shell_script_path_is_relative_to_spec(tmp_path):
    spec_dir = tmp_path / "project"
    script = spec_dir / "scripts" / "print-value.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho spec-script\n")
    res = _step_shell(
        _ctx(tmp_path, spec_path=str(spec_dir / "fabrikant.yaml")),
        {"type": "shell", "cmd": "sh scripts/print-value.sh"},
        NOOP,
    )
    assert res["ok"] is True
    assert "spec-script" in res["output"]


def test_shell_script_in_worktree_stays_relative(tmp_path):
    """A script the worktree has itself runs from the worktree (the code under test), not the spec's copy."""
    spec_dir = tmp_path / "project"
    (spec_dir / "scripts").mkdir(parents=True)
    (spec_dir / "scripts" / "which.sh").write_text("echo spec-copy\n")
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    (work / "scripts" / "which.sh").write_text("echo worktree-copy\n")
    res = _step_shell(
        {"workdir": str(work), "spec_path": str(spec_dir / "fabrikant.yaml")},
        {"type": "shell", "cmd": "sh scripts/which.sh"},
        NOOP,
    )
    assert res["ok"] is True
    assert "worktree-copy" in res["output"] and "spec-copy" not in res["output"]


def test_shell_timeout(tmp_path):
    """Timeout wirft subprocess.TimeoutExpired (wird von der Engine gefangen)."""
    import subprocess

    with pytest.raises(subprocess.TimeoutExpired):
        _step_shell(_ctx(tmp_path), {"type": "shell", "cmd": "sleep 5", "timeout": 1}, NOOP)


def test_shell_timeout_kills_process_tree(tmp_path):
    """Timeout beendet auch vom Befehl gestartete Prozesse (Regression: verwaiste
    `python -m fabrikant.relay`/`git push` nach Ablauf, nur `sh` wurde getötet)."""
    import subprocess
    import time

    pidfile = tmp_path / "pid"
    step = {"type": "shell", "cmd": f"sleep 30 & echo $! > {pidfile}; wait", "timeout": 1}
    with pytest.raises(subprocess.TimeoutExpired):
        _step_shell(_ctx(tmp_path), step, NOOP)
    pid = int(pidfile.read_text())
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"Grandchild {pid} lebt nach dem Timeout noch")


# ---------------------------------------------------------------- env-Mechanismus
def test_shell_env_substitution(tmp_path):
    res = _step_shell(
        _ctx(tmp_path, label="ready to develop"),
        {"type": "shell", "env": {"lbl": "{label}"}, "cmd": 'echo "$lbl"'},
        NOOP,
    )
    assert res["ok"] is True
    assert "ready to develop" in res["output"]


def test_shell_env_carries_untrusted_text_safely(tmp_path):
    """Untrusted Text (Newlines, Quotes, Backticks, $-Sequenzen) bleibt über env: intakt.

    Regression (Issue #62): Der Triage-Loop substituierte LLM-`acceptance_criteria`
    inline in den Shell-Befehl → je nach LLM-Formulierung stilles No-op (exit 0)
    oder Command-Substitution. Über `env:` ist der Text ein einzelnes, inertes
    Argument — die Shell interpretiert ihn nicht.
    """
    # Enthält alle Trigger aus Issue #62: Backticks, Quotes mit Newline in
    # unquoted Region (zerhackt Inline-Wörter), $-Sequenzen, Single-Quotes.
    evil = (
        "- Kriterium 1 mit `touch " + str(tmp_path / "pwned") + '` und "Quote\n'
        "weiter" + "\" sowie 'Single' und $HOME und $(echo injected)\n"
        "- Kriterium 3"
    )
    out = tmp_path / "out.txt"
    step = {
        "type": "shell",
        "env": {"criteria": "{criteria}"},
        "cmd": 'if [ -n "$criteria" ]; then printf "%s" "$criteria" > out.txt; fi',
    }
    res = _step_shell(_ctx(tmp_path, criteria=evil), step, NOOP)
    assert res["ok"] is True
    assert out.read_text() == evil, "Text muss byte-gleich erhalten bleiben ($() bleibt literal)"
    assert not (tmp_path / "pwned").exists(), "Backtick-Command darf nicht ausgeführt werden"


# ---------------------------------------------------------------- Inline-Guard (fail-fast)
def test_shell_fails_on_placeholder_env_key_collision(tmp_path):
    """Mischmuster (Inline-Placehalter in cmd kollidiert mit env:-Key) → fail-fast (Issue #62).

    Nur WARNen reicht nicht: die Warnung ist ignorierbar und die
    Inline-Substitution würde trotzdem laufen. Der Step muss klar fehlschlagen.
    """
    emitted = []
    marker = tmp_path / "ran"
    res = _step_shell(
        _ctx(tmp_path, label="ready to develop"),
        {"type": "shell", "env": {"lbl": "{label}"}, "cmd": f"touch {marker}; echo $lbl {{lbl}}"},
        emitted.append,
    )
    assert res["ok"] is False
    assert res["exit_code"] == 1
    assert "env:" in res["summary"]
    assert "{lbl}" in res["summary"], "Kollidierender Platzhalter muss in der Fehlermeldung genannt werden"
    assert res.get("reason") == "placeholder-env-clash", "Maschinenlesbarer Fehler-Code für Automation"
    assert not marker.exists(), "Befehl darf nicht ausgeführt werden"
    assert any("ERROR" in m and "env:" in m for m in emitted), "Mischmuster muss als ERROR gemeldet werden"


def test_shell_allows_unrelated_inline_placeholders_with_env(tmp_path):
    """env: + Inline-Placehalter OHNE Kollision mit env:-Keys → erlaubt.

    Vertrauenswürdige Placehalter ({issue_number}) dürfen inline stehen, auch
    wenn derselbe Step env: für untrusted Werte nutzt (Trust-Policy, ADR 0008).
    """
    emitted = []
    res = _step_shell(
        _ctx(tmp_path, issue_number="62", criteria="Kriterium 1"),
        {"type": "shell", "env": {"criteria": "{criteria}"}, "cmd": 'echo issue {issue_number} "$criteria"'},
        emitted.append,
    )
    assert res["ok"] is True
    assert "issue 62" in res["output"]
    assert "Kriterium 1" in res["output"]
    assert not any("ERROR" in m for m in emitted), "Kein Guard-Trigger ohne Kollision"


def test_shell_guard_allows_inline_placeholder_absent_from_env(tmp_path):
    """Inline-Placehalter, der NICHT in env: vorkommt (auch untrusted) → erlaubt.

    Der Guard erkennt nur Kollisionen (Placehalter ∩ env:-Keys desselben
    Steps). Ob ein Placehalter vertrauenswürdig ist, kann die Runtime nicht
    wissen — das regelt die Trust-Policy (shell.py-Docstring, ADR 0008):
    LLM-/Issue-Kontext gehört in env:, nicht in cmd. Ohne Kollision läuft die
    Substitution wie gewohnt weiter.
    """
    emitted = []
    res = _step_shell(
        _ctx(tmp_path, criteria="Kriterium 1", label="ready"),
        {"type": "shell", "env": {"lbl": "{label}"}, "cmd": 'echo "{criteria}" "$lbl"'},
        emitted.append,
    )
    assert res["ok"] is True
    assert "Kriterium 1" in res["output"], "Substitution läuft weiter (kein Kollisions-Fall)"
    assert not any("ERROR" in m for m in emitted), "Kein Guard-Trigger ohne Kollision mit env:-Keys"


def test_shell_guard_ignores_literal_braces(tmp_path):
    """Literal-Braces, die KEIN Platzhalter sind (z. B. JSON) → kein Guard-Trigger."""
    emitted = []
    res = _step_shell(
        _ctx(tmp_path),
        {"type": "shell", "env": {"lbl": "x"}, "cmd": 'echo \'{"a": 1}\' "$lbl"'},
        emitted.append,
    )
    assert res["ok"] is True
    assert not any("ERROR" in m for m in emitted)


def test_shell_guard_ignores_digit_leading_braces(tmp_path):
    """Braces mit Ziffer am Anfang ({123}) sind KEIN Identifikator → kein Guard-Trigger.

    Die geteilte Platzhalter-Regex (context._PLACEHOLDER_RE) verlangt einen
    Buchstaben/Unterstrich an erster Stelle — {123} ist kein Platzhalter und
    darf den Guard nicht fälschlich triggern (kein False-Positive).
    """
    emitted = []
    res = _step_shell(
        _ctx(tmp_path),
        {"type": "shell", "env": {"lbl": "x"}, "cmd": 'echo "{123}" "$lbl"'},
        emitted.append,
    )
    assert res["ok"] is True
    assert not any("ERROR" in m for m in emitted)


def test_shell_no_guard_env_only(tmp_path):
    """Reines env:-Muster (keine Placehalter in cmd) → kein Guard-Trigger."""
    emitted = []
    res = _step_shell(
        _ctx(tmp_path, label="ready to develop"),
        {"type": "shell", "env": {"lbl": "{label}"}, "cmd": 'echo "$lbl"'},
        emitted.append,
    )
    assert res["ok"] is True
    assert not any("ERROR" in m for m in emitted)


def test_shell_no_guard_inline_only(tmp_path):
    """Inline-Substitution ohne env: (vertrauenswürdige Config-Werte) → kein Guard-Trigger."""
    emitted = []
    res = _step_shell(
        _ctx(tmp_path, issue_number="62"),
        {"type": "shell", "cmd": "echo issue {issue_number}"},
        emitted.append,
    )
    assert res["ok"] is True
    assert not any("ERROR" in m for m in emitted)


# ---------------------------------------------------------------- Log-Masking
def test_shell_logs_template_not_substituted(tmp_path):
    """Log-Masking: emit loggt das Template (mit {placeholders}), NICHT den
    substituierten Befehl — substituierte Werte können Secrets enthalten."""
    emitted = []
    secret = "super-secret-token-value"
    res = _step_shell(
        _ctx(tmp_path, token=secret),
        {"type": "shell", "cmd": "echo {token}"},
        emitted.append,
    )
    assert res["ok"] is True
    log_lines = [m for m in emitted if m.startswith("  $ ")]
    assert log_lines, "Befehl muss geloggt werden"
    assert "{token}" in log_lines[0], "Template mit Platzhalter muss geloggt werden"
    assert secret not in "".join(emitted), "Substituierter Wert (Secret) darf nicht im Log stehen"


def test_shell_redacts_secrets(tmp_path, monkeypatch):
    """Secrets in env-Werten werden aus stdout/stderr-Output redacted.

    Der Step maskiert output/summary selbst (Defense-in-Depth, secrets._redact);
    die Engine maskiert zusätzlich beim Record-Write. Ein Secret (env-Var mit
    Secret-Name) darf weder im output- noch im summary-Feld landen.
    """
    secret = "super-secret-value-123"
    monkeypatch.setattr(secrets, "_env_secrets", [secret])
    res = _step_shell(
        _ctx(tmp_path, token=secret),
        {"type": "shell", "env": {"tok": "{token}"}, "cmd": 'echo "$tok"'},
        NOOP,
    )
    assert res["ok"] is True
    assert secret not in res["output"], "Secret darf nicht im output-Feld stehen"
    assert secret not in res["summary"], "Secret darf nicht im summary-Feld stehen"
    assert "[REDACTED]" in res["output"], "Secret muss im output-Feld redacted sein"


# ---------------------------------------------------------------- mktemp-Cleanup (add-acceptance-Pattern)
def _mktemp_step(tmp_path, extra: str = "") -> dict:
    """add-acceptance-Pattern aus projects/fabrikant.fabrikant.yaml: mktemp + trap EXIT."""
    return {
        "type": "shell",
        "env": {"acceptance_criteria": "{acceptance_criteria}"},
        "cmd": (
            "body_file=$(mktemp /tmp/fabrikant-triage-body.XXXXXX)\n"
            "trap 'rm -f \"$body_file\"' EXIT\n"
            'printf \'%s\\n\' "$acceptance_criteria" > "$body_file"\n'
            f'echo "$body_file" > {tmp_path / "path.txt"}\n' + extra
        ),
    }


def test_shell_mktemp_body_file_cleaned_up(tmp_path):
    """body_file (mktemp) wird nach erfolgreicher Ausführung entfernt (trap EXIT)."""
    res = _step_shell(_ctx(tmp_path, acceptance_criteria="Kriterium 1"), _mktemp_step(tmp_path), NOOP)
    assert res["ok"] is True
    body_file = (tmp_path / "path.txt").read_text().strip()
    assert body_file.startswith("/tmp/fabrikant-triage-body.")
    assert not os.path.exists(body_file), "body_file muss nach der Ausführung entfernt sein"


def test_shell_mktemp_body_file_cleaned_up_on_error(tmp_path):
    """body_file (mktemp) wird auch auf dem Fehlerpfad entfernt (trap EXIT)."""
    res = _step_shell(_ctx(tmp_path, acceptance_criteria="Kriterium 1"), _mktemp_step(tmp_path, "exit 7\n"), NOOP)
    assert res["ok"] is False
    assert res["exit_code"] == 7
    body_file = (tmp_path / "path.txt").read_text().strip()
    assert not os.path.exists(body_file), "body_file muss auch bei Fehler entfernt sein"


# ---------------------------------------------------------------- Inline-Fallen (Dokumentation)
def test_shell_inline_multiline_mixed_quotes_silently_noops(tmp_path):
    """DIE Falle (Issue #62): Multi-Line-Wert mit Quotes → Wort wird am Newline
    zerhackt → `[`-Test bricht zur Laufzeit → if false → exit 0 → „pass“.

    Der Step meldet Erfolg, obwohl nichts passiert ist — genau das, was bei
    Issue #62 passiert ist (Criteria fehlten, Step „pass“ in 0.05 s).
    """
    res = _step_shell(
        _ctx(tmp_path, criteria='a "b\nc" d'),
        {"type": "shell", "cmd": 'if [ -n "{criteria}" ]; then echo appended; fi'},
        NOOP,
    )
    assert res["ok"] is True, "exit 0 trotz gebrochenem Test — die Falle"
    assert "appended" not in res["output"], "…aber der then-Block lief nie"


def test_shell_inline_backticks_execute_commands(tmp_path):
    """Inline-Backticks im substituierten Text werden als Command ausgeführt (Injection)."""
    marker = tmp_path / "pwned"
    res = _step_shell(
        _ctx(tmp_path, criteria=f"`touch {marker}`"),
        {"type": "shell", "cmd": 'echo "{criteria}"'},
        NOOP,
    )
    assert res["ok"] is True
    assert marker.exists(), "Command-Substitution wurde ausgeführt — deshalb: env:, nicht inline"


# ---------------------------------------------------------------- Verdikt
def test_shell_verdict_line_becomes_field_and_leaves_output_and_summary(tmp_path):
    cmd = "echo work; echo '::fabrikant-verdict::fail'; echo 'relay #7 audit: fail'"
    res = _step_shell(_ctx(tmp_path), {"type": "shell", "cmd": cmd}, NOOP)
    assert res["ok"] is True and res["verdict"] == "fail"
    assert res["summary"] == "relay #7 audit: fail"
    assert "fabrikant-verdict" not in res["output"] and "work" in res["output"]


def test_shell_without_verdict_has_no_field(tmp_path):
    res = _step_shell(_ctx(tmp_path), {"type": "shell", "cmd": "echo hi"}, NOOP)
    assert "verdict" not in res


# ---------------------------------------------------------------- github_identity (ADR 0010)
def test_shell_github_identity_sets_role_token(tmp_path, monkeypatch):
    """`github_identity: reviewer` → `gh` in the command runs with that role's token."""
    monkeypatch.setattr(
        "fabrikant.steps.shell.github_env", lambda role: {"PATH": os.environ["PATH"], "GH_TOKEN": f"tok-{role}"}
    )
    step = {"type": "shell", "cmd": 'echo "$GH_TOKEN"', "github_identity": "reviewer"}
    res = _step_shell(_ctx(tmp_path), step, NOOP)
    assert res["ok"] is True
    assert "tok-reviewer" in res["output"]


def test_shell_github_identity_missing_token_fails_as_environment(tmp_path, monkeypatch):
    def _missing(role):
        raise RuntimeError("GITHUB_REVIEWER_TOKEN not available")

    monkeypatch.setattr("fabrikant.steps.shell.github_env", _missing)
    res = _step_shell(_ctx(tmp_path), {"type": "shell", "cmd": "true", "github_identity": "reviewer"}, NOOP)
    assert res["ok"] is False
    assert res["failure_kind"] == "environment"
    assert "GITHUB_REVIEWER_TOKEN" in res["summary"]


# ---------------------------------------------------------------- sandboxed (ADR 0054)
def _capture_run_shell(monkeypatch):
    calls = []

    def fake(session_id, cmd, workdir, env, timeout):
        calls.append({"cmd": cmd, "workdir": workdir, "env": env})
        return subprocess.CompletedProcess([], 0, "in-container\n", "")

    monkeypatch.setattr("fabrikant.sandbox.run_shell", fake)
    return calls


def test_shell_sandboxed_runs_in_container_with_scrubbed_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRIKANT_SANDBOX", "1")
    monkeypatch.setenv("SOME_HOST_SECRET", "s3cret")
    monkeypatch.setattr(
        "fabrikant.steps.shell.github_env",
        lambda role: {"GH_TOKEN": "tok", "GITHUB_TOKEN": "tok", "FABRIKANT_ENV_FILE": "/h"},
    )
    calls = _capture_run_shell(monkeypatch)
    step = {"type": "shell", "cmd": "make lint", "sandboxed": True, "github_identity": "dev", "env": {"X": "{v}"}}
    res = _step_shell(_ctx(tmp_path, v="val"), step, NOOP)
    assert res["ok"] is True and "in-container" in res["output"]
    env = calls[0]["env"]
    assert env["X"] == "val" and env["GH_TOKEN"] == "tok"
    assert "SOME_HOST_SECRET" not in env and "FABRIKANT_ENV_FILE" not in env  # agent allowlist (ADR 0050)


def test_shell_sandboxed_without_identity_gets_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRIKANT_SANDBOX", "1")
    monkeypatch.setenv("GH_TOKEN", "host-token")
    monkeypatch.setenv("GITHUB_TOKEN", "host-token")
    calls = _capture_run_shell(monkeypatch)
    res = _step_shell(_ctx(tmp_path), {"type": "shell", "cmd": "make test", "sandboxed": True}, NOOP)
    assert res["ok"] is True
    assert "GH_TOKEN" not in calls[0]["env"] and "GITHUB_TOKEN" not in calls[0]["env"]


def test_shell_sandboxed_without_global_switch_runs_on_host(tmp_path, monkeypatch):
    monkeypatch.delenv("FABRIKANT_SANDBOX", raising=False)
    calls = _capture_run_shell(monkeypatch)
    res = _step_shell(_ctx(tmp_path), {"type": "shell", "cmd": "echo host", "sandboxed": True}, NOOP)
    assert res["ok"] is True and "host" in res["output"] and calls == []


def test_shell_sandbox_error_fails_as_environment(tmp_path, monkeypatch):
    from fabrikant import sandbox

    monkeypatch.setenv("FABRIKANT_SANDBOX", "1")

    def boom(*a, **k):
        raise sandbox.SandboxError("result rejected: symlink")

    monkeypatch.setattr("fabrikant.sandbox.run_shell", boom)
    res = _step_shell(_ctx(tmp_path), {"type": "shell", "cmd": "true", "sandboxed": True}, NOOP)
    assert res["ok"] is False and res["failure_kind"] == "environment" and "symlink" in res["summary"]
