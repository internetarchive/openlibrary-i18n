#!/usr/bin/env python3
"""
reconcile_rerun.py -- decide what reconcile.yml should do about an i18n/ai-* PR
whose required checks are NOT all settled.

Why this exists
---------------
reconcile.yml merges an open AI PR once every required check passes. A required
check can be ABSENT from `gh pr checks` for two very different reasons:

  * its run is still in flight (CI legitimately running) -- wait; or
  * the run that would post it DIED AT STARTUP -- a zero-job "workflow file
    issue" run posts no check at all, so the required check never appears.

The old loop could not tell these apart: both looked like "not settled", so it
printed "checks pending -- leaving for the next run" and moved on. A startup
failure therefore left the PR in SILENT, PERMANENT limbo -- it never merged and
never went red. That is exactly how PR #230 stranded (2026-11-09): a transient
startup failure of "Verify translations deploy cleanly" left the check absent,
and every 6-hourly reconcile pass re-classified it as pending forever.

Three states, never two
-----------------------
A required check is PRESENT+PASSING (handled upstream: merge), PRESENT+FAILED
(handled upstream: settled -> left red for a human/re-translation), or
INDETERMINATE-ABSENT. This module owns only the third, and it must resolve to
one of:

  rerun     re-dispatch the specific startup-failed run(s) that would post an
            absent required check. A transient blip self-heals.
  escalate  stop re-running and surface the PR to a human. The absence is
            persistent (reruns keep startup-failing) or unexplained on a PR old
            enough that CI should have reported -- an indeterminate state must
            reach a person, not loop silently.
  wait      a required check is genuinely in flight, or a fresh PR's checks have
            not reported yet -- do nothing; the next pass re-evaluates.

Safety (why the rerun is narrowly scoped)
-----------------------------------------
A zero-job startup failure has a second, look-alike cause: an UNAPPROVED FORK
run (conclusion ``action_required``) that needs a maintainer's click. Auto-
rerunning that would thrash and could mask a real approval gate. So a run is
re-dispatched ONLY when the PR is same-repo and bot-authored (the i18n/ai-*
invariant) AND the run's conclusion is a transient failure -- never
``action_required``. The gate is explicit so widening the path later stays safe.

This is the decision only. reconcile.yml gathers the inputs with ``gh`` and
executes the result (``gh run rerun`` / label+comment / nothing).
"""
from __future__ import annotations

import argparse
import json
import sys

RERUN = "rerun"
ESCALATE = "escalate"
WAIT = "wait"

PASS = "pass"
_PENDING = {"", "pending"}

# gh run conclusions that are a transient/startup failure worth re-dispatching.
RERUNNABLE_CONCLUSIONS = {"failure", "startup_failure", "cancelled", "timed_out", "stale"}
# A run awaiting a maintainer's approval is NOT transient; never auto-rerun it.
APPROVAL_CONCLUSIONS = {"action_required"}
# A run still executing -- do not rerun, just wait.
LIVE_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}


def _classify(buckets):
    """Classify one check's set of gh buckets: pass / pending / failed / absent."""
    if buckets is None:
        return "absent"
    if buckets == {PASS}:
        return "pass"
    if buckets & _PENDING:
        return "pending"
    return "failed"  # any terminal non-pass bucket (fail, cancel, skipping, ...)


def _buckets_by_name(checks):
    out: dict[str, set[str]] = {}
    for c in checks:
        out.setdefault(c.get("name"), set()).add((c.get("bucket") or "").lower())
    return out


def _run_for(name, runs):
    """The most-recent run (max attempt) whose name matches this check, or None."""
    matching = [r for r in runs if r.get("name") == name]
    if not matching:
        return None
    return max(matching, key=lambda r: int(r.get("attempt") or 1))


def decide(required, checks, runs, *, author, cross_repo, pr_is_old,
           bot_login="openlibrary-bot", max_attempts=3):
    """Return (action, payload, reason).

    - ``required``: required check names.
    - ``checks``: ``gh pr checks --json name,bucket`` array.
    - ``runs``: runs for the PR HEAD SHA, each ``{name, databaseId, status,
      conclusion, attempt}``.
    - ``author``: PR author login; ``cross_repo``: head repo != base repo.
    - ``pr_is_old``: PR old enough that CI should have reported by now.

    Payload is a sorted list of databaseIds for RERUN, a sorted list of check
    names for ESCALATE, and ``[]`` for WAIT. Priority RERUN > ESCALATE > WAIT:
    a rerun can still make progress, so it is tried before giving up to a human.
    """
    required = list(dict.fromkeys(required))
    by_name = _buckets_by_name(checks)
    transient_eligible = (not cross_repo) and (author == bot_login)

    rerun_ids: set[int] = set()
    escalate: list[str] = []
    waiting: list[str] = []
    failed: list[str] = []

    for name in required:
        state = _classify(by_name.get(name))
        if state == "pass":
            continue
        if state == "failed":
            failed.append(name)
            continue
        if state == "pending":
            waiting.append(f"{name!r} in flight")
            continue

        # state == "absent": decide from the run that should have posted it.
        run = _run_for(name, runs)
        if run is None:
            # No run produced this check at all.
            if pr_is_old:
                escalate.append(name)  # should have run by now -> a human looks
            else:
                waiting.append(f"{name!r} not reported yet")
            continue

        conclusion = (run.get("conclusion") or "").lower()
        status = (run.get("status") or "").lower()
        attempt = int(run.get("attempt") or 1)

        if status in LIVE_STATUSES or (status != "completed" and not conclusion):
            waiting.append(f"{name!r} run is live")
        elif conclusion == "success":
            # The run that posts this check SUCCEEDED, but the check is not in the
            # rollup yet -- status-propagation lag, not a real absence. Wait for it
            # to appear; never escalate a healthy PR over a visibility delay.
            waiting.append(f"{name!r} run succeeded; check not in rollup yet (lag)")
        elif conclusion in APPROVAL_CONCLUSIONS or not transient_eligible:
            # Fork-approval gate or a non-bot/fork PR: not ours to auto-rerun.
            waiting.append(f"{name!r} needs approval / not transient-eligible")
        elif conclusion in RERUNNABLE_CONCLUSIONS:
            if attempt < max_attempts:
                rerun_ids.add(int(run["databaseId"]))
            else:
                escalate.append(name)  # reruns exhausted -> persistent -> human
        else:
            # Terminal non-pass that is not rerunnable (e.g. a real failure that
            # somehow left the check absent) -> indeterminate -> a human looks.
            escalate.append(name)

    # A required check that positively FAILED means the PR is red regardless of
    # the absent ones; do not merge and do not rerun -- leave it for the settled
    # (red) path / a re-translation.
    if failed:
        return WAIT, [], "a required check failed (" + ", ".join(sorted(failed)) + "); PR is red"
    if rerun_ids:
        return RERUN, sorted(rerun_ids), "re-dispatching startup-failed run(s) for absent required check(s)"
    if escalate:
        return ESCALATE, sorted(dict.fromkeys(escalate)), "required check absent and not self-healing"
    return WAIT, [], ("; ".join(waiting) or "nothing to do")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--require", action="append", default=[], metavar="CHECK")
    p.add_argument("--checks", metavar="FILE", help="gh pr checks JSON (default stdin)")
    p.add_argument("--runs", metavar="FILE", required=True, help="head-SHA runs JSON")
    p.add_argument("--author", required=True)
    p.add_argument("--cross-repo", default="false")
    p.add_argument("--pr-old", default="false")
    p.add_argument("--max-attempts", type=int, default=3)
    args = p.parse_args(argv)

    if not args.require:
        print("reconcile_rerun: no --require checks given", file=sys.stderr)
        return 2

    def _bool(s):
        return str(s).strip().lower() in {"true", "1", "yes"}

    try:
        checks_raw = open(args.checks, encoding="utf-8").read() if args.checks else sys.stdin.read()
        checks = json.loads(checks_raw) if checks_raw.strip() else []
        runs = json.loads(open(args.runs, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"reconcile_rerun: bad input ({exc})", file=sys.stderr)
        return 2
    if not isinstance(checks, list) or not isinstance(runs, list):
        print("reconcile_rerun: checks and runs must be JSON arrays", file=sys.stderr)
        return 2

    action, payload, reason = decide(
        args.require, checks, runs,
        author=args.author, cross_repo=_bool(args.cross_repo),
        pr_is_old=_bool(args.pr_old), max_attempts=args.max_attempts,
    )
    # Machine-readable first line: "<ACTION> <space-joined payload>"; reason on stderr.
    print(f"{action} " + " ".join(str(x) for x in payload))
    print(f"{action}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
