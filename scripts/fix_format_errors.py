#!/usr/bin/env python3
"""Clear msgstr for entries with format-string mismatches or placeholder incompatibilities.

Usage: python scripts/fix_format_errors.py <lang>
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
LOCALE_DIR = REPO_ROOT / "locale"


def named_placeholders(s):
    return set(re.findall(r'%\((\w+)\)[sdfr]', s))


def positional_placeholders(s):
    return re.findall(r'(?<!%)%[sdfr]', s)


def has_mismatch(msgid, msgstr):
    if not msgstr:
        return False
    msgid_str = msgid[0] if isinstance(msgid, tuple) else msgid
    msgstr_str = msgstr[0] if isinstance(msgstr, tuple) and msgstr[0] else (msgstr if isinstance(msgstr, str) else '')

    id_named = named_placeholders(msgid_str)
    str_named = named_placeholders(msgstr_str)

    if id_named - str_named:
        return True
    if str_named - id_named:
        return True

    id_pos = positional_placeholders(msgid_str)
    str_pos = positional_placeholders(msgstr_str)
    if id_named and str_pos and not str_named:
        return True
    if str_named and id_pos and not id_named:
        return True
    if len(id_pos) != len(str_pos):
        return True

    if isinstance(msgid, tuple) and isinstance(msgstr, tuple):
        for form in msgstr:
            if not form:
                continue
            form_named = named_placeholders(form)
            if id_named - form_named:
                return True

    return False


def has_incompatible_plural(msgid, msgstr):
    if isinstance(msgid, tuple) and isinstance(msgstr, tuple):
        if len(msgstr) >= 2 and msgstr[0] and not msgstr[1]:
            return True
        if not any(msgstr):
            return True
    return False


def fix_language(lang):
    from babel.messages.pofile import read_po, write_po

    po_path = LOCALE_DIR / lang / "messages.po"
    with open(po_path, "rb") as f:
        cat = read_po(f)

    cleared = 0
    for msg in cat:
        if not msg.id or not msg.string:
            continue
        if has_mismatch(msg.id, msg.string) or has_incompatible_plural(msg.id, msg.string):
            msg.string = '' if not isinstance(msg.id, tuple) else ('', '')
            msg.flags.discard('fuzzy')
            cleared += 1

    with open(po_path, "wb") as f:
        write_po(f, cat, width=10000, omit_header=False)
    print(f"{lang}: cleared {cleared} entries")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: fix_format_errors.py <lang>")
    fix_language(sys.argv[1])
