"""Truth table for scripts/land_strategy.py.

native_merge is taken ONLY when the contributor's head is exactly what should land
(mergeable with no conflict AND the heal changed nothing), so ``gh pr merge`` lands the
validated content and shows "Merged" -- forks included. Everything else (a conflict, or
the heal changed the content) lands on base + closes, because showing "Merged" would
require rewriting a head branch, which the workflow never does. These tests pin every
cell and the CLI (which accepts GitHub's own mergeable strings).
"""
from __future__ import annotations

import importlib.util
import itertools
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "land_strategy.py"
_spec = importlib.util.spec_from_file_location("land_strategy", SCRIPT)
ls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ls)

choose = ls.choose_strategy


def test_clean_mergeable_is_native_merge_same_repo():
    assert choose(mergeable=True, heal_changed=False, cross_repo=False) == ls.NATIVE_MERGE


def test_clean_mergeable_is_native_merge_fork():
    # A clean fork PR needing no heal merges natively -- the merge button needs no write
    # access to the fork, so it shows "Merged" and credits the author.
    assert choose(mergeable=True, heal_changed=False, cross_repo=True) == ls.NATIVE_MERGE


def test_heal_changed_lands_on_base():
    # #197 / #212 shape: the content had to change to land -> never a native merge of a
    # now-stale head; land the validated content on base.
    assert choose(mergeable=True, heal_changed=True, cross_repo=True) == ls.LAND_ON_BASE
    assert choose(mergeable=True, heal_changed=True, cross_repo=False) == ls.LAND_ON_BASE


def test_conflict_never_native_merges():
    for heal_changed, cross in itertools.product([True, False], repeat=2):
        assert choose(mergeable=False, heal_changed=heal_changed,
                      cross_repo=cross) != ls.NATIVE_MERGE


def test_native_merge_requires_mergeable_and_no_heal():
    for mergeable, heal_changed, cross in itertools.product([True, False], repeat=3):
        if choose(mergeable, heal_changed, cross) == ls.NATIVE_MERGE:
            assert mergeable is True and heal_changed is False


def test_cross_repo_does_not_change_the_decision():
    # cross_repo is recorded but not decisive; the same (mergeable, heal_changed) gives
    # the same strategy for fork and same-repo.
    for mergeable, heal_changed in itertools.product([True, False], repeat=2):
        assert choose(mergeable, heal_changed, cross_repo=True) == \
               choose(mergeable, heal_changed, cross_repo=False)


# --- CLI the workflow calls ---------------------------------------------------

def _cli(mergeable, heal_changed, cross="false"):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--mergeable", mergeable,
         "--heal-changed", heal_changed, "--cross-repo", cross],
        capture_output=True, text=True,
    )


def test_cli_accepts_github_mergeable_strings():
    r = _cli("MERGEABLE", "false", "true")
    assert r.returncode == 0 and r.stdout.strip() == ls.NATIVE_MERGE, r.stderr
    r = _cli("CONFLICTING", "true", "true")
    assert r.returncode == 0 and r.stdout.strip() == ls.LAND_ON_BASE, r.stderr
    # UNKNOWN (GitHub still computing mergeability) is treated as not-mergeable (safe).
    r = _cli("UNKNOWN", "false", "false")
    assert r.returncode == 0 and r.stdout.strip() == ls.LAND_ON_BASE, r.stderr


def test_cli_rejects_garbage():
    r = _cli("maybe", "false", "false")
    assert r.returncode != 0
