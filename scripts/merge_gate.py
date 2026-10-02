#!/usr/bin/env python3
"""
merge_gate.py -- decide whether translate.yml may auto-merge a PR.

translate.yml creates a PR of machine translations and then, in the same job,
decides whether to merge it. This repository has allow_auto_merge:false and
`main` has no branch protection, so nothing except this gate stands between
unreviewed AI output and `main`. The gate therefore merges ONLY when every
required status check has actually passed. A check that is absent, pending,
failed, skipped or cancelled is never read as a pass -- "no result for X" is
not "X passed".

Two modes, so the workflow can both wait for checks and then decide:

  --mode merge    (default) exit 0 iff every required check is present and
                  passing; else exit 1. This is the merge decision.
  --mode settled  exit 0 iff every required check has reached a final verdict
                  (present and not pending); else exit 1. The workflow polls on
                  this to tell "still running / not reported yet" (keep waiting)
                  apart from "finished, but not all passing" (give up now).

Usage, from the workflow:

    gh pr checks "$pr" --json name,bucket \
      | python3 scripts/merge_gate.py --require validate --require bake-regression

  exit 0  the mode's condition holds
  exit 1  it does not (reason on stderr)
  exit 2  misuse: no --require given, or the input was not a JSON array of
          objects, or --input could not be read (also not a merge)

Input is the JSON array `gh pr checks --json name,bucket` emits, read from stdin
(or --input FILE). `bucket` is gh's own rollup of a check's state into exactly
one of pass / fail / pending / skipping / cancel; it is compared
case-insensitively, and only "pass" counts as a pass.
"""
from __future__ import annotations

import argparse
import json
import sys

PASS = "pass"
NOT_SETTLED = {"", "pending"}  # no final verdict yet (or no bucket reported)


def _buckets_by_name(checks):
    """name -> set of (lower-cased) buckets seen for that check."""
    buckets_by_name: dict[str, set[str]] = {}
    for check in checks:
        name = check.get("name")
        bucket = (check.get("bucket") or "").lower()
        buckets_by_name.setdefault(name, set()).add(bucket)
    return buckets_by_name


def decide(checks, required):
    """Return (merge: bool, reason: str).

    A required check is satisfied only if it appears at least once and *every*
    appearance is a pass -- so a re-run that is still pending, or a failed
    duplicate, blocks.
    """
    required = list(dict.fromkeys(required))  # de-dupe, preserve order
    buckets_by_name = _buckets_by_name(checks)

    reasons = []
    for name in required:
        buckets = buckets_by_name.get(name)
        if buckets is None:
            reasons.append(f"required check {name!r} is absent (no result reported)")
        elif buckets != {PASS}:
            not_passing = sorted(b or "<none>" for b in buckets if b != PASS)
            reasons.append(
                f"required check {name!r} is not passing "
                f"(seen: {', '.join(not_passing)})"
            )

    if reasons:
        return False, "; ".join(reasons)
    return True, "all required checks passed: " + ", ".join(required)


def settled(checks, required):
    """Return (settled: bool, reason: str).

    Settled means every required check is present and has a final verdict (no
    pending/unknown bucket), so waiting longer cannot change the decision. A
    required check that is absent or still pending is NOT settled.
    """
    required = list(dict.fromkeys(required))
    buckets_by_name = _buckets_by_name(checks)

    waiting = []
    for name in required:
        buckets = buckets_by_name.get(name)
        if buckets is None:
            waiting.append(f"{name!r} not reported yet")
        elif buckets & NOT_SETTLED:
            waiting.append(f"{name!r} still pending")
    if waiting:
        return False, "; ".join(waiting)
    return True, "all required checks have reached a final verdict"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require", action="append", default=[], metavar="CHECK",
        help="name of a check that must pass before merge; repeat for each",
    )
    parser.add_argument(
        "--mode", choices=("merge", "settled"), default="merge",
        help="merge: decide whether to merge (default); "
             "settled: whether every required check has a final verdict",
    )
    parser.add_argument(
        "--input", metavar="FILE",
        help="read the check JSON from FILE instead of stdin",
    )
    args = parser.parse_args(argv)

    if not args.require:
        print("merge_gate: no --require checks given; refusing to merge",
              file=sys.stderr)
        return 2

    try:
        raw = open(args.input, encoding="utf-8").read() if args.input else sys.stdin.read()
    except OSError as exc:
        print(f"merge_gate: could not read input ({exc}); refusing to merge",
              file=sys.stderr)
        return 2
    try:
        checks = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        print(f"merge_gate: could not parse check JSON ({exc}); refusing to merge",
              file=sys.stderr)
        return 2
    if not isinstance(checks, list) or not all(isinstance(c, dict) for c in checks):
        print("merge_gate: expected a JSON array of check objects; refusing to merge",
              file=sys.stderr)
        return 2

    if args.mode == "settled":
        ok, reason = settled(checks, args.require)
        label = "SETTLED" if ok else "NOT SETTLED"
    else:
        ok, reason = decide(checks, args.require)
        label = "MERGE" if ok else "NO MERGE"

    print(f"{label}: {reason}", file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
