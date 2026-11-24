"""runmodel: Übergangstabellen (alle erlaubten und alle verbotenen Übergänge), Fehlertyp, Run-Eigenschaften."""

import itertools

import pytest

from fabrikant.runmodel import (
    APPROVAL_TRANSITIONS,
    RUN_TRANSITIONS,
    STEP_TRANSITIONS,
    ApprovalStatus,
    InvalidTransition,
    Run,
    RunStatus,
    StepStatus,
    check_transition,
)

MACHINES = [
    (RunStatus, RUN_TRANSITIONS),
    (StepStatus, STEP_TRANSITIONS),
    (ApprovalStatus, APPROVAL_TRANSITIONS),
]

# Erwartete Übergänge, unabhängig von den Tabellen ausgeschrieben (ADR 0034 Nr. 2).
EXPECTED_RUN = {
    "queued": {"running", "error"},
    "running": {"needs_approval", "pass", "fail", "error"},
    "needs_approval": {"running", "rejected", "error"},
    "pass": set(),
    "fail": set(),
    "error": set(),
    "rejected": set(),
}
EXPECTED_STEP = {
    "running": {"waiting", "pass", "fail", "timeout", "error"},
    "waiting": {"running"},
    "pass": set(),
    "fail": set(),
    "timeout": set(),
    "error": set(),
    "skipped": set(),
}
EXPECTED_APPROVAL = {
    "pending": {"queued", "approved", "rejected"},
    "queued": {"approved", "rejected", "pending"},
    "approved": set(),
    "rejected": set(),
}


@pytest.mark.parametrize(("enum_cls", "table"), MACHINES)
def test_table_covers_every_status(enum_cls, table):
    assert set(table) == set(enum_cls)


@pytest.mark.parametrize(
    ("enum_cls", "table", "expected"),
    [
        (RunStatus, RUN_TRANSITIONS, EXPECTED_RUN),
        (StepStatus, STEP_TRANSITIONS, EXPECTED_STEP),
        (ApprovalStatus, APPROVAL_TRANSITIONS, EXPECTED_APPROVAL),
    ],
)
def test_all_pairs_allowed_or_forbidden(enum_cls, table, expected):
    for current, target in itertools.product(enum_cls, repeat=2):
        if target.value in expected[current.value]:
            assert check_transition(table, current, target) is target
        else:
            with pytest.raises(InvalidTransition):
                check_transition(table, current, target)


def test_invalid_transition_message_names_states():
    with pytest.raises(InvalidTransition, match=r"pass → running"):
        check_transition(RUN_TRANSITIONS, RunStatus.PASS, RunStatus.RUNNING)


def test_invalid_transition_is_value_error():
    assert issubclass(InvalidTransition, ValueError)


def test_queued_and_tier_approval_are_real_states():
    assert RunStatus.QUEUED in RUN_TRANSITIONS
    assert ApprovalStatus.QUEUED in APPROVAL_TRANSITIONS


def test_run_properties():
    run = Run(id="1", project="p", loop="l", status=RunStatus.NEEDS_APPROVAL, waiting_step="tier-approval")
    assert run.awaiting_tier_approval and not run.is_terminal
    gate = Run(id="1", project="p", loop="l", status=RunStatus.NEEDS_APPROVAL, waiting_step="deploy")
    assert not gate.awaiting_tier_approval
    done = Run(id="1", project="p", loop="l", status=RunStatus.PASS, waiting_step="tier-approval")
    assert done.is_terminal and not done.awaiting_tier_approval


# ---------------------------------------------------------------- Verdikt (fachliches Step-Ergebnis)
def test_verdict_line_round_trips_and_rejects_invalid_words():
    from fabrikant.runmodel import split_verdict, verdict_line

    assert split_verdict(f"a\n{verdict_line('fail')}\nrelay done\n") == ("a\nrelay done\n", "fail")
    assert split_verdict(f"{verdict_line('pass')}\n{verdict_line('blocked')}")[1] == "blocked"  # letztes gilt
    assert split_verdict("::fabrikant-verdict::BAD\nx") == ("x", "")  # ungültig: entfernt, aber kein Verdikt
    assert split_verdict("say ::fabrikant-verdict::fail inline") == ("say ::fabrikant-verdict::fail inline", "")
    for bad in ("", "Fail", "x" * 21, "a b"):
        with pytest.raises(ValueError):
            verdict_line(bad)


def test_run_verdict_is_the_last_reported_step_verdict():
    from fabrikant.runmodel import Run, RunStatus, Step, StepStatus

    steps = (
        Step("a", "shell", StepStatus.PASS, verdict="pass"),
        Step("b", "shell", StepStatus.PASS, verdict="fail"),
        Step("c", "shell", StepStatus.PASS),
    )
    assert Run(id="r", project="p", loop="l", status=RunStatus.PASS, steps=steps).verdict == "fail"
    assert Run(id="r", project="p", loop="l", status=RunStatus.PASS).verdict == ""
