"""Truth table for scripts/reconcile_rerun.py.

The regression this pins: a required check that is ABSENT because the run that
would post it died at startup (zero-job "workflow file issue") must NOT be read
as "pending, wait forever" (PR #230's silent strand). It must resolve to a
RERUN of that transient run, and -- once reruns are exhausted or the absence is
otherwise unexplained on an old PR -- to ESCALATE (a human-visible state). Only
a check genuinely in flight yields WAIT.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "reconcile_rerun.py"
_spec = importlib.util.spec_from_file_location("reconcile_rerun", SCRIPT)
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

REQUIRED = ["validate", "Verify translations deploy cleanly"]
VERIFY = "Verify translations deploy cleanly"


def _checks(**bucket_by_name):
    return [{"name": n, "bucket": b} for n, b in bucket_by_name.items()]


def _run(name, *, conclusion="", status="completed", attempt=1, dbid=999):
    return {"name": name, "databaseId": dbid, "status": status,
            "conclusion": conclusion, "attempt": attempt}


def _decide(checks, runs, **kw):
    kw.setdefault("author", "openlibrary-bot")
    kw.setdefault("cross_repo", False)
    kw.setdefault("pr_is_old", True)
    return rr.decide(REQUIRED, checks, runs, **kw)


# --- the #230 regression -----------------------------------------------------

def test_absent_required_from_startup_failed_run_is_rerun_not_wait():
    # validate passed; the deploy-clean check is ABSENT because its run startup-
    # failed (zero jobs). Old loop said WAIT forever; we must RERUN that run.
    checks = _checks(validate="pass", review="skipping")  # VERIFY absent
    runs = [_run(VERIFY, conclusion="startup_failure", attempt=1, dbid=42672057083)]
    action, payload, _ = _decide(checks, runs)
    assert action == rr.RERUN
    assert payload == [42672057083]


def test_prove_the_old_behavior_would_strand_it():
    # Same shape, but with NO run visible to rerun and the PR not old yet -> the
    # only safe call is WAIT. This is the state the old loop was permanently
    # stuck in; here it is explicitly the transient, fresh-PR case only.
    checks = _checks(validate="pass")  # VERIFY absent
    action, payload, _ = _decide(checks, runs=[], pr_is_old=False)
    assert action == rr.WAIT and payload == []


# --- bound + escalate --------------------------------------------------------

def test_reruns_exhausted_escalates():
    checks = _checks(validate="pass")
    runs = [_run(VERIFY, conclusion="startup_failure", attempt=3)]
    action, payload, _ = _decide(checks, runs, max_attempts=3)
    assert action == rr.ESCALATE and payload == [VERIFY]


def test_absent_with_no_run_on_old_pr_escalates():
    # Required check never got a run at all, and the PR is old enough that CI
    # should have reported -> indeterminate -> a human, not perpetual pending.
    checks = _checks(validate="pass")
    action, payload, _ = _decide(checks, runs=[], pr_is_old=True)
    assert action == rr.ESCALATE and payload == [VERIFY]


def test_absent_with_no_run_on_fresh_pr_waits():
    checks = _checks(validate="pass")
    action, _, _ = _decide(checks, runs=[], pr_is_old=False)
    assert action == rr.WAIT


# --- genuinely in flight = wait ----------------------------------------------

def test_absent_but_run_in_progress_waits():
    checks = _checks(validate="pass")
    runs = [_run(VERIFY, conclusion="", status="in_progress")]
    action, _, _ = _decide(checks, runs)
    assert action == rr.WAIT


def test_present_pending_bucket_waits():
    checks = _checks(validate="pass", **{VERIFY: "pending"})
    action, _, _ = _decide(checks, runs=[])
    assert action == rr.WAIT


def test_absent_check_with_a_successful_run_waits_not_escalates():
    # The run that posts the check SUCCEEDED but the check has not propagated to
    # the rollup yet (status-propagation lag). A healthy PR must NOT be escalated
    # over a visibility delay -- even on an old PR.
    checks = _checks(validate="pass")  # VERIFY absent
    runs = [_run(VERIFY, conclusion="success")]
    action, payload, reason = _decide(checks, runs, pr_is_old=True)
    assert action == rr.WAIT and payload == []


# --- safety: never rerun the approval / fork kind ----------------------------

def test_action_required_run_is_never_rerun():
    checks = _checks(validate="pass")
    runs = [_run(VERIFY, conclusion="action_required")]
    action, _, _ = _decide(checks, runs)
    assert action == rr.WAIT


def test_cross_repo_fork_is_not_transient_eligible():
    checks = _checks(validate="pass")
    runs = [_run(VERIFY, conclusion="startup_failure")]
    action, _, _ = _decide(checks, runs, cross_repo=True)
    assert action == rr.WAIT  # a fork startup-fail is an approval gate, not ours


def test_non_bot_author_is_not_transient_eligible():
    checks = _checks(validate="pass")
    runs = [_run(VERIFY, conclusion="startup_failure")]
    action, _, _ = _decide(checks, runs, author="some-contributor")
    assert action == rr.WAIT


# --- a positively-failed required check leaves the PR red (no rerun) ----------

def test_failed_required_check_is_left_red_not_rerun():
    # VERIFY present+failed, validate absent -> the PR is red; do not rerun the
    # absent one, do not merge. (Upstream settled path marks it red.)
    checks = _checks(validate="pass", **{VERIFY: "fail"})
    runs = [_run("validate", conclusion="startup_failure")]
    action, payload, reason = _decide(checks, runs)
    assert action == rr.WAIT and "red" in reason


def test_all_pass_is_wait_nothing_to_do():
    checks = _checks(validate="pass", **{VERIFY: "pass"})
    action, _, _ = _decide(checks, runs=[])
    assert action == rr.WAIT


def test_rerun_prefers_most_recent_attempt_of_a_run():
    # Two run records for the same check (older attempt + newer); the bound reads
    # the newest attempt, so attempt=3 exhausts even if an attempt=1 row exists.
    checks = _checks(validate="pass")
    runs = [_run(VERIFY, conclusion="startup_failure", attempt=1, dbid=1),
            _run(VERIFY, conclusion="startup_failure", attempt=3, dbid=1)]
    action, payload, _ = _decide(checks, runs, max_attempts=3)
    assert action == rr.ESCALATE


# --- CLI ---------------------------------------------------------------------

def _cli(checks, runs, tmp_path, **flags):
    cfile = tmp_path / "checks.json"
    rfile = tmp_path / "runs.json"
    cfile.write_text(json.dumps(checks))
    rfile.write_text(json.dumps(runs))
    argv = [sys.executable, str(SCRIPT), "--checks", str(cfile), "--runs", str(rfile),
            "--author", flags.get("author", "openlibrary-bot"),
            "--cross-repo", flags.get("cross_repo", "false"),
            "--pr-old", flags.get("pr_old", "true")]
    for r in REQUIRED:
        argv += ["--require", r]
    return subprocess.run(argv, capture_output=True, text=True)


def test_cli_emits_rerun_line(tmp_path):
    checks = _checks(validate="pass")
    runs = [_run(VERIFY, conclusion="startup_failure", attempt=1, dbid=777)]
    r = _cli(checks, runs, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "rerun 777"


def test_cli_emits_wait_line(tmp_path):
    checks = _checks(validate="pass", **{VERIFY: "pending"})
    r = _cli(checks, [], tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip() == "wait"
