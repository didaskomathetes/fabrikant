"""fabrikant.excerpt: failure-aware excerpt for the run log (regression: crash hidden behind a noisy tail)."""

from __future__ import annotations

from fabrikant.excerpt import failure_excerpt, failure_reason


def test_short_output_comes_back_whole():
    ex = failure_excerpt("a\nb\nc")
    assert ex.lines == ("a", "b", "c") and ex.omitted == 0
    assert ex.render() == "a\nb\nc"


def test_crash_dump_survives_noisy_tail():
    """Segfault in the middle of the output, followed by hundreds of package installation lines."""
    out = [f"+ pkg{i}==1.0" for i in range(300)]
    out += ["Fatal Python error: Segmentation fault", "", "Current thread 0x1 (most recent call first):"]
    out += [f'  File "x.py", line {i}' for i in range(5)]
    out += [f"+ more{i}==2.0" for i in range(300)]
    out += ["rot: src/app"]
    ex = failure_excerpt("\n".join(out), max_lines=60, tail=10)
    text = ex.render()
    assert "Fatal Python error: Segmentation fault" in text
    assert "Current thread 0x1 (most recent call first):" in text
    assert ex.lines[-1] == "rot: src/app"
    assert ex.lines[0] == "… (298 Zeilen)"  # window starts BEFORE=2 lines before the marker
    assert text.endswith(f"[Auszug: {ex.total - ex.omitted} von {ex.total} Zeilen; voller Output im Run-Record]")
    assert len(ex.lines) - sum(line.startswith("… (") for line in ex.lines) == ex.total - ex.omitted


def test_budget_keeps_earliest_markers_and_always_the_tail():
    out = []
    for i in range(50):
        out += [f"FAILED test_{i}"] + [f"noise {i}.{j}" for j in range(20)]
    out += ["end"]
    ex = failure_excerpt("\n".join(out), max_lines=40, tail=5)
    shown = [line for line in ex.lines if not line.startswith("… (")]
    assert len(shown) == 40
    assert "FAILED test_0" in shown and "FAILED test_49" not in shown
    assert ex.lines[-1] == "end"


def test_without_markers_it_is_a_marked_tail():
    ex = failure_excerpt("\n".join(str(i) for i in range(200)), max_lines=20, tail=20)
    assert ex.lines[0] == "… (180 Zeilen)" and ex.lines[-1] == "199" and ex.omitted == 180


def test_excerpt_text_in_english(monkeypatch):
    monkeypatch.setenv("FABRIKANT_LANG", "en")
    ex = failure_excerpt("\n".join(str(i) for i in range(200)), max_lines=20, tail=20)
    assert ex.lines[0] == "… (180 lines)"
    assert ex.render().endswith("[excerpt: 20 of 200 lines; full output in the run record]")


def test_failure_reason_is_last_marker_line():
    assert failure_reason("1 failed, 3 passed\n851 passed\nrot: src/a\n") == "rot: src/a"
    assert failure_reason("all good\n10 passed") == ""
