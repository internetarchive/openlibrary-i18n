"""Tests for scripts/merge_gate.py.

The gate decides whether translate.yml may auto-merge a machine-translation PR.
Only an explicit pass of every required check is a pass: a check that is absent,
pending, failed, skipped or cancelled must never let the PR merge. These tests
feed the gate hand-built check lists (the shape `gh pr checks --json name,bucket`
emits) and assert merge vs no-merge, and they exercise the CLI entry point the
workflow actually invokes, not just the functions.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_SCRIPT = Path(os.environ.get("MERGE_GATE_SCRIPT", _ROOT / "scripts" / "merge_gate.py"))
_spec = importlib.util.spec_from_file_location("merge_gate", _SCRIPT)
mg = importlib.util.module_from_spec(_spec)
sys.modules["merge_gate"] = mg
_spec.loader.exec_module(mg)

REQUIRED = ["validate", "bake-regression"]


def _check(name: str, bucket: str) -> dict:
    return {"name": name, "bucket": bucket}


# --- decide(): the merge decision -----------------------------------------

def test_both_required_pass_merges():
    checks = [_check("validate", "pass"), _check("bake-regression", "pass")]
    merge, reason = mg.decide(checks, REQUIRED)
    assert merge is True, reason


def test_unrelated_passing_checks_do_not_block():
    checks = [
        _check("validate", "pass"),
        _check("bake-regression", "pass"),
        _check("some-other-ci", "pass"),
    ]
    merge, _ = mg.decide(checks, REQUIRED)
    assert merge is True


@pytest.mark.parametrize("bad_bucket", ["fail", "pending", "skipping", "cancel"])
def test_a_required_check_not_passing_blocks(bad_bucket):
    for broken in REQUIRED:
        checks = [_check(n, bad_bucket if n == broken else "pass") for n in REQUIRED]
        merge, reason = mg.decide(checks, REQUIRED)
        assert merge is False, f"{broken}={bad_bucket} should block but merged"
        assert broken in reason


def test_a_missing_required_check_blocks():
    # The three-state trap: "no bake-regression result" is NOT "bake-regression
    # passed". Only validate is present.
    checks = [_check("validate", "pass")]
    merge, reason = mg.decide(checks, REQUIRED)
    assert merge is False
    assert "bake-regression" in reason


def test_no_checks_at_all_blocks():
    merge, reason = mg.decide([], REQUIRED)
    assert merge is False
    assert "validate" in reason and "bake-regression" in reason


def test_duplicate_runs_must_all_pass():
    # Same check name twice (e.g. a re-run): one pass + one fail must block.
    checks = [
        _check("validate", "pass"),
        _check("validate", "fail"),
        _check("bake-regression", "pass"),
    ]
    merge, reason = mg.decide(checks, REQUIRED)
    assert merge is False
    assert "validate" in reason


def test_duplicate_runs_all_passing_merges():
    checks = [
        _check("validate", "pass"),
        _check("validate", "pass"),
        _check("bake-regression", "pass"),
    ]
    merge, _ = mg.decide(checks, REQUIRED)
    assert merge is True


def test_missing_bucket_field_is_not_a_pass():
    checks = [_check("validate", "pass"), {"name": "bake-regression"}]
    merge, _ = mg.decide(checks, REQUIRED)
    assert merge is False


# --- settled(): "has a final verdict been reached?" -----------------------

def test_settled_when_all_terminal_pass():
    checks = [_check("validate", "pass"), _check("bake-regression", "pass")]
    ok, _ = mg.settled(checks, REQUIRED)
    assert ok is True


def test_settled_when_all_terminal_even_if_failing():
    # Finished and failing is settled: stop waiting, leave the PR open.
    checks = [_check("validate", "fail"), _check("bake-regression", "pass")]
    ok, _ = mg.settled(checks, REQUIRED)
    assert ok is True
    # ... and decide() must still refuse to merge it.
    merge, _ = mg.decide(checks, REQUIRED)
    assert merge is False


def test_not_settled_while_pending():
    checks = [_check("validate", "pending"), _check("bake-regression", "pass")]
    ok, reason = mg.settled(checks, REQUIRED)
    assert ok is False
    assert "validate" in reason


def test_not_settled_while_a_required_check_is_absent():
    checks = [_check("validate", "pass")]  # bake-regression not reported yet
    ok, reason = mg.settled(checks, REQUIRED)
    assert ok is False
    assert "bake-regression" in reason


def test_not_settled_on_empty():
    ok, _ = mg.settled([], REQUIRED)
    assert ok is False


# --- the CLI: the artifact the workflow actually runs ---------------------

def _run_cli(payload: str, *extra: str):
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--require", "validate",
         "--require", "bake-regression", *extra],
        input=payload, capture_output=True, text=True,
    )


def test_cli_merges_on_all_pass():
    payload = json.dumps([_check("validate", "pass"), _check("bake-regression", "pass")])
    r = _run_cli(payload)
    assert r.returncode == 0, r.stderr
    assert "MERGE" in r.stdout


def test_cli_blocks_on_failing_check():
    payload = json.dumps([_check("validate", "fail"), _check("bake-regression", "pass")])
    r = _run_cli(payload)
    assert r.returncode == 1
    assert "NO MERGE" in r.stderr


def test_cli_blocks_on_missing_check():
    payload = json.dumps([_check("validate", "pass")])
    r = _run_cli(payload)
    assert r.returncode == 1
    assert "bake-regression" in r.stderr


def test_cli_settled_mode_exit_codes():
    both_done = json.dumps([_check("validate", "fail"), _check("bake-regression", "pass")])
    r = _run_cli(both_done, "--mode", "settled")
    assert r.returncode == 0 and "SETTLED" in r.stdout
    pending = json.dumps([_check("validate", "pending"), _check("bake-regression", "pass")])
    r = _run_cli(pending, "--mode", "settled")
    assert r.returncode == 1 and "NOT SETTLED" in r.stderr


def test_cli_blocks_on_empty_input():
    r = _run_cli("")
    assert r.returncode == 1


def test_cli_misuse_exit_2_on_non_json():
    r = _run_cli("not json")
    assert r.returncode == 2


def test_cli_misuse_exit_2_on_non_object_elements():
    # gh always emits objects; anything else is a broken caller, not a merge.
    r = _run_cli(json.dumps(["validate", "bake-regression"]))
    assert r.returncode == 2
    r = _run_cli(json.dumps([None, {"name": "validate", "bucket": "pass"}]))
    assert r.returncode == 2


def test_cli_misuse_exit_2_on_top_level_object():
    r = _run_cli(json.dumps({"name": "validate", "bucket": "pass"}))
    assert r.returncode == 2


def test_cli_misuse_exit_2_on_bad_input_file():
    r = subprocess.run(
        [sys.executable, str(_SCRIPT), "--require", "validate",
         "--input", "/no/such/file/merge_gate_test.json"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2


def test_cli_refuses_when_no_required_checks_given():
    r = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=json.dumps([_check("validate", "pass")]),
        capture_output=True, text=True,
    )
    assert r.returncode == 2  # a gate with nothing to require must not merge
