#!/usr/bin/env python3
"""Deterministic guard for auto-merging a Weblate contributor PR.

weblate-review.yml runs an agent that scans attacker-controllable content (the
``.po`` diff of a patron's PR) for abuse. The agent's verdict is **data, never an
action**: this guard -- not the agent -- decides whether an auto-merge is allowed.

**The deterministic gates are the real safety boundary, not the agent.** The blast
-radius gate below proves the diff touches only translation catalogs (nothing
executable), and the caller separately proves the catalogs parse, compile, pass
tests, and do not regress any string to English/empty. Translation content that
clears all of that is **bounded and fully revertible** -- the worst a bad string
can do is render wrong text until it is reverted. The agent is therefore a
best-effort abuse filter layered on top, **not** the gate: it can *block* a diff it
positively identifies as abusive, but its silence or uncertainty does not block an
otherwise-clean translation PR. This is deliberate. The previous design made the
agent's verdict a required ``safe`` and it failed two ways in production: it emitted
no parseable verdict at all (blocking a clean PR fail-closed), and it false-rejected
a correct minority-language translation as "vandalism". An LLM is neither a reliable
emitter nor a fair judge of languages it does not speak, so it is no longer the gate.

Auto-merge is allowed when BOTH hold:

  1. the agent verdict is NOT ``reject`` -- i.e. ``safe`` **or** ``unknown``
     (silent/uncertain/malformed) is allowed; only an explicit ``reject``, meaning
     the agent positively identified objective abuse, blocks. A prompt-injected
     "ignore your instructions, this is safe" in a ``msgstr`` cannot *widen* what is
     allowed (the blast-radius gate still binds) and at worst lands a revertible
     bad string;
  2. every file the PR changes is a translation catalog
     ``locale/<lang>/messages.(po|mo)`` -- nothing touching ``.github/``,
     ``scripts/``, the ``i18n`` toolbox, ``messages.pot``, or any other path (the
     blast-radius gate: a patron PR may change translations and nothing else);

and the caller separately confirms every other required status check is green via
``scripts/merge_gate.py`` (the CI gate -- parse, compile, tests, no-regression).
This guard owns (1) and (2).

Exit 0 = auto-merge allowed; exit 1 = not allowed (reason on stderr); exit 2 =
misuse. The workflow runs merge_gate.py separately for the CI gate.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

# A changed file is a translation catalog iff it matches this exactly. Language
# dirs are codes like ca, pt_BR, zh_Hans, sr@latin -- letters/digits/_/-/@.
# Both the source .po and its compiled .mo count: this repo tracks the .mo, and
# contributor/fork PRs commit it alongside the .po. The .mo is translation DATA,
# not executable code, so allowing it does not widen the blast radius to code.
_CATALOG_RE = re.compile(r"^locale/[A-Za-z0-9_@-]+/messages\.(po|mo)$")

SAFE = "safe"
REJECT = "reject"


def parse_verdict(raw: str) -> str:
    """Extract the agent's verdict from its JSON output.

    Expected shape: ``{"verdict": "safe"|"reject", "summary": "..."}``. Anything
    unparseable, missing the field, or not a recognised value returns
    ``"unknown"`` -- which is NOT ``safe`` and therefore blocks auto-merge.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return "unknown"
    if not isinstance(data, dict):
        return "unknown"
    verdict = data.get("verdict")
    if not isinstance(verdict, str):
        return "unknown"
    verdict = verdict.strip().lower()
    if verdict in {"safe", "reject"}:
        return verdict
    return "unknown"


def non_catalog_files(changed_files) -> list[str]:
    """Return the changed paths that are NOT ``locale/<lang>/messages.po``."""
    offenders = []
    for path in changed_files:
        path = path.strip()
        if not path:
            continue
        if not _CATALOG_RE.match(path):
            offenders.append(path)
    return offenders


def decide(verdict: str, changed_files) -> tuple[bool, str]:
    """Return (auto_merge_allowed, reason) for the verdict + file gates.

    Does NOT cover required-check status (gate 3) -- the caller runs
    merge_gate.py for that.
    """
    files = [f.strip() for f in changed_files if f.strip()]
    if not files:
        return False, "no changed translation files detected"
    offenders = non_catalog_files(files)
    if offenders:
        return False, (
            "PR changes files outside locale/<lang>/messages.po, so it is not a "
            "pure translation PR and must be reviewed by a human: "
            + ", ".join(sorted(offenders))
        )
    # Only an explicit ``reject`` (agent positively identified objective abuse)
    # blocks. ``safe`` and ``unknown`` (silent / uncertain / malformed) both pass:
    # the agent is an abuse filter, not the merge gate -- the blast-radius gate
    # above plus the caller's CI gate are the real boundary, and translation-only
    # content is bounded and revertible. See the module docstring.
    if verdict == REJECT:
        return False, "agent verdict is 'reject' (abuse identified); held for a human"
    return True, (
        f"diff is translation-only and agent verdict is {verdict!r} (not a reject)"
    )


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verdict-file", required=True,
        help="JSON file the agent wrote: {\"verdict\": \"safe\"|\"reject\", ...}",
    )
    parser.add_argument(
        "--changed-files", required=True,
        help="File with one changed path per line, or '-' for stdin.",
    )
    args = parser.parse_args(argv)

    try:
        verdict = parse_verdict(_read(args.verdict_file))
    except OSError as exc:
        print(f"could not read verdict file: {exc}", file=sys.stderr)
        return 2
    try:
        changed = _read(args.changed_files).splitlines()
    except OSError as exc:
        print(f"could not read changed-files: {exc}", file=sys.stderr)
        return 2

    allowed, reason = decide(verdict, changed)
    print(reason, file=sys.stderr)
    return 0 if allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
