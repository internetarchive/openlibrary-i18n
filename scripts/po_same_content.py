#!/usr/bin/env python3
"""Exit 0 if two PO files have the same translations, 1 if not.

Compares msgid, context, msgstr, flags and obsolete entries. Ignores the header,
comments, locations, order and wrapping.
"""
from __future__ import annotations

import sys

from babel.messages.pofile import read_po


def content(path: str) -> tuple[dict, dict]:
    with open(path, "rb") as f:
        catalog = read_po(f)

    def entries(messages):
        # The header is the message with an empty id.
        return {(m.id, m.context): (m.string, frozenset(m.flags)) for m in messages if m.id}

    return entries(catalog), entries(catalog.obsolete.values())


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: po_same_content.py A.po B.po", file=sys.stderr)
        return 2
    return 0 if content(argv[0]) == content(argv[1]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
