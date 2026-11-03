#!/usr/bin/env python3
"""Choose HOW to land an auto-healed, already-validated translation PR.

By the time this runs, the enforce job has produced a healed working tree that passed
every mechanical gate (translation-only blast-radius, resolve/fix/de-regress, validate,
compile, PO tests, check-scope, in-job bake-regression). The only question left is
*how* to put that content on the base branch so the result looks like a normal
contribution wherever GitHub allows it.

The constraint that forces the choice: GitHub shows a PR as **"Merged"** (crediting the
contributor via the normal merge button) ONLY when the PR's own head branch is merged.
We can do that safely only when the contributor's head is *already* the content we want
to land -- i.e. it merges cleanly AND the heal did not have to change anything. Then
``gh pr merge`` lands exactly the validated content and credits the author, and it works
for forks too (the merge button needs no write access to the fork).

If the heal had to change the content (a conflict, or a repair/de-regress), the head is
no longer what should land. Making GitHub still say "Merged" would mean rewriting the
head branch to the healed content -- a force-push / checkout of the PR head, which for a
fork is untrusted and which this workflow deliberately never does. So in that case we
commit the validated healed content to base with ``Closes #`` and close the PR. GitHub
shows that "Closed", not "Merged"; it is the safe, universal path, and the workflow
posts a comment explaining that the work did land and why the badge differs.

Two strategies, then:

  native_merge   mergeable as-is AND the heal was a no-op on the touched langs
                 -> ``gh pr merge`` the head. Shows "Merged"; works for forks.

  land_on_base   anything else (a conflict, or the heal changed the content)
                 -> commit the healed content to base + ``Closes #`` and close.

This module is the decision only; the workflow computes the two inputs from git/gh and
executes the result, falling back to land_on_base if a native merge fails at runtime.
``cross_repo`` is accepted (and recorded) but does not change the decision today -- it
documents that head-rewriting for forks was considered and deliberately not done.
"""
from __future__ import annotations

import argparse

NATIVE_MERGE = "native_merge"
LAND_ON_BASE = "land_on_base"


def choose_strategy(mergeable: bool, heal_changed: bool, cross_repo: bool = False) -> str:
    """Pick the land strategy.

    - ``mergeable``: GitHub reports the PR has no merge conflict with base.
    - ``heal_changed``: the heal altered the touched-language content vs the PR head
      (True = the head is NOT already the deploy-clean/current content).
    - ``cross_repo``: the head is on a fork (recorded, not decisive -- see module doc).

    Native merge is safe only when the head is exactly what should land: mergeable with
    no conflict AND the heal changed nothing. Otherwise the validated content differs
    from the head, so we land it on base and close (never rewrite a head branch).
    """
    if mergeable and not heal_changed:
        return NATIVE_MERGE
    return LAND_ON_BASE


def _b(s: str) -> bool:
    v = s.strip().lower()
    if v in {"true", "1", "yes", "mergeable"}:
        return True
    if v in {"false", "0", "no", "conflicting", "unknown", "dirty", "draft", ""}:
        return False
    raise argparse.ArgumentTypeError(f"expected a boolean-ish value, got {s!r}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mergeable", type=_b, required=True,
                   help="true if GitHub reports the PR mergeable (no conflict)")
    p.add_argument("--heal-changed", type=_b, required=True,
                   help="true if the heal altered the touched-language content vs the head")
    p.add_argument("--cross-repo", type=_b, default=False,
                   help="true if the head is on a fork (recorded, not decisive)")
    args = p.parse_args(argv)
    print(choose_strategy(args.mergeable, args.heal_changed, args.cross_repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
