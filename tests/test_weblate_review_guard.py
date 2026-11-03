"""Tests for scripts/weblate_review_guard.py.

The guard is the deterministic barrier that makes the agentic reviewer's verdict
un-abusable: an auto-merge is allowed ONLY when the verdict is exactly "safe" AND
every changed file is a translation catalog. These tests pin both gates and the
three-state verdict parsing (safe / reject / unknown), and exercise the CLI the
workflow invokes.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "weblate_review_guard.py"
_spec = importlib.util.spec_from_file_location("weblate_review_guard", SCRIPT)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


# --- verdict parsing (three-state; never default to safe) -------------------

def test_parse_safe():
    assert guard.parse_verdict('{"verdict": "safe", "summary": "ok"}') == "safe"


def test_parse_reject():
    assert guard.parse_verdict('{"verdict": "reject", "summary": "spam"}') == "reject"


def test_parse_is_case_insensitive_and_trims():
    assert guard.parse_verdict('{"verdict": "  SAFE "}') == "safe"


def test_parse_unrecognised_value_is_unknown():
    assert guard.parse_verdict('{"verdict": "probably fine"}') == "unknown"


def test_parse_missing_field_is_unknown():
    assert guard.parse_verdict('{"summary": "no verdict here"}') == "unknown"


def test_parse_malformed_json_is_unknown():
    assert guard.parse_verdict("not json at all") == "unknown"
    assert guard.parse_verdict("") == "unknown"
    assert guard.parse_verdict("[1,2,3]") == "unknown"


# --- file (blast-radius) gate ------------------------------------------------

def test_translation_catalog_paths_accepted():
    assert guard.non_catalog_files(["locale/ca/messages.po"]) == []
    assert guard.non_catalog_files(
        ["locale/pt_BR/messages.po", "locale/zh_Hans/messages.po"]
    ) == []
    # the compiled .mo is translation data and is tracked/committed by fork PRs
    assert guard.non_catalog_files(["locale/es/messages.mo"]) == []
    assert guard.non_catalog_files(
        ["locale/sc/messages.po", "locale/sc/messages.mo"]
    ) == []


def test_non_catalog_paths_flagged():
    assert guard.non_catalog_files([".github/workflows/x.yml"]) == [
        ".github/workflows/x.yml"
    ]
    assert guard.non_catalog_files(["scripts/merge_gate.py"]) == ["scripts/merge_gate.py"]
    # the source template is not a translation
    assert guard.non_catalog_files(["messages.pot"]) == ["messages.pot"]
    # a stray file inside a locale dir is still not a catalog
    assert guard.non_catalog_files(["locale/ca/evil.sh"]) == ["locale/ca/evil.sh"]


# --- combined decision -------------------------------------------------------

def test_safe_and_translation_only_is_allowed():
    allowed, _ = guard.decide("safe", ["locale/ca/messages.po", "locale/sq/messages.po"])
    assert allowed is True


def test_reject_blocks_even_if_translation_only():
    allowed, reason = guard.decide("reject", ["locale/ca/messages.po"])
    assert allowed is False
    assert "not 'safe'" in reason


def test_unknown_verdict_blocks():
    allowed, _ = guard.decide("unknown", ["locale/ca/messages.po"])
    assert allowed is False


def test_safe_verdict_cannot_override_a_structural_file():
    # The injection scenario: agent coerced to "safe", but the diff smuggles a
    # workflow change. The file gate must still block it.
    allowed, reason = guard.decide(
        "safe", ["locale/ca/messages.po", ".github/workflows/translate.yml"]
    )
    assert allowed is False
    assert ".github/workflows/translate.yml" in reason


def test_empty_diff_blocks():
    allowed, _ = guard.decide("safe", [])
    assert allowed is False


# --- CLI the workflow calls --------------------------------------------------

def _run_cli(tmp_path, verdict_json, changed_lines):
    vf = tmp_path / "verdict.json"
    vf.write_text(verdict_json, encoding="utf-8")
    cf = tmp_path / "changed.txt"
    cf.write_text("\n".join(changed_lines), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--verdict-file", str(vf), "--changed-files", str(cf)],
        capture_output=True, text=True,
    )


def test_cli_allows_safe_translation_only(tmp_path):
    r = _run_cli(tmp_path, '{"verdict": "safe"}', ["locale/ca/messages.po"])
    assert r.returncode == 0, r.stderr


def test_cli_blocks_injected_structural_change(tmp_path):
    r = _run_cli(
        tmp_path, '{"verdict": "safe"}',
        ["locale/ca/messages.po", "scripts/merge_gate.py"],
    )
    assert r.returncode == 1
    assert "scripts/merge_gate.py" in r.stderr


def test_cli_blocks_reject(tmp_path):
    r = _run_cli(tmp_path, '{"verdict": "reject"}', ["locale/ca/messages.po"])
    assert r.returncode == 1
