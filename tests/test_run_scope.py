"""Tests for the workflow run-scope decision (tools/run_scope.py).

Regression cover for the 2026-09-21 silent-idle defect: three scheduled runs
executed zero work steps and reported success because they read the bot's own
"[auto]" commit subject and concluded they were an echo of themselves.
"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("run_scope", os.path.join(ROOT, "tools", "run_scope.py"))
run_scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_scope)


AUTO = "[auto] collect + pipeline + site build 2026-09-21T23:58:24Z"
HUMAN = "Research page corrections: no duplicate baseline row"


def test_schedule_run_after_auto_commit_still_works():
    d = run_scope.decide("schedule", AUTO)
    assert d == {"skip": False, "probe": False}, "a cron run must never skip because of the head commit"
    assert run_scope.verify("schedule", d["skip"], d["probe"], daily_outcome="success") == []


def test_manual_dispatch_after_auto_commit_still_works():
    # An operator dispatching a backfill right after an auto commit used to be ignored.
    d = run_scope.decide("workflow_dispatch", AUTO)
    assert d == {"skip": False, "probe": False}
    assert run_scope.verify("workflow_dispatch", False, False, daily_outcome="success") == []


def test_push_of_own_auto_commit_may_skip():
    d = run_scope.decide("push", AUTO)
    assert d == {"skip": True, "probe": False}


def test_push_probe_commit_takes_probe_path():
    d = run_scope.decide("push", "[probe] diagnostics only")
    assert d == {"skip": True, "probe": True}
    assert run_scope.verify("push", True, True) == []


def test_push_of_human_commit_does_full_work():
    d = run_scope.decide("push", HUMAN)
    assert d == {"skip": False, "probe": False}


def test_subject_matching_is_first_line_only_and_exact_tag():
    # A body line mentioning [auto] must not change the decision, and near-misses
    # (automatic, [autofix]) are not the bot tag.
    assert run_scope.decide("push", HUMAN + "\n\nsee [auto] commits") == {"skip": False, "probe": False}
    assert run_scope.decide("push", "automatic cleanup") == {"skip": False, "probe": False}
    assert run_scope.decide("push", "[autofix] typo") == {"skip": False, "probe": False}


def test_verify_flags_the_silent_idle_state():
    problems = run_scope.verify("schedule", skip=True, probe=False, daily_outcome="skipped")
    assert problems, "skip without probe must be fatal"
    assert any("report success having done nothing" in p for p in problems)
    assert any("must never take the skip path" in p for p in problems)


def test_verify_flags_schedule_run_that_skipped_collection():
    problems = run_scope.verify("schedule", skip=False, probe=False, daily_outcome="skipped")
    assert any("skipped the daily collection step" in p for p in problems)


def test_verify_flag_failed_collection_but_accepts_push_scope():
    assert run_scope.verify("schedule", False, False, daily_outcome="failure")
    assert run_scope.verify("push", True, False) == []      # bot echo, nothing expected
    assert run_scope.verify("push", False, False) == []     # human push, full work expected


def test_cli_writes_output_lines(tmp_path):
    out = tmp_path / "gh_output"
    rc = run_scope.main(["--event", "schedule", "--message", AUTO, "--out", str(out)])
    assert rc == 0
    assert out.read_text().strip() == "skip=false\nprobe=false"


def test_cli_verify_exit_codes():
    assert run_scope.main(["--event", "schedule", "--verify", "--skip", "true",
                           "--probe", "false"]) == 1
    assert run_scope.main(["--event", "schedule", "--verify", "--skip", "false",
                           "--probe", "false", "--daily-outcome", "success"]) == 0
