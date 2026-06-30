#!/usr/bin/env python3
"""Auto-add missing HTML attributes to msgstr by comparing with msgid.

Usage: python scripts/fix_html_attrs.py <lang>
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
LOCALE_DIR = REPO_ROOT / "locale"


def extract_attrs(tag_str):
    return dict(re.findall(r'([\w-]+)=["\']([^"\']*)["\']', tag_str))


def fix_attrs(msgid_str, msgstr_str):
    id_tags = re.findall(r'<([a-zA-Z][^>]*)>', msgid_str)
    str_tags = re.findall(r'<([a-zA-Z][^>]*)>', msgstr_str)
    if len(id_tags) != len(str_tags):
        return msgstr_str
    result = msgstr_str
    for id_tag, str_tag in zip(id_tags, str_tags):
        id_name = id_tag.split()[0] if id_tag.split() else ''
        str_name = str_tag.split()[0] if str_tag.split() else ''
        if id_name.lower() != str_name.lower():
            continue
        id_attrs = extract_attrs(id_tag)
        str_attrs = extract_attrs(str_tag)
        missing = {k: v for k, v in id_attrs.items() if k not in str_attrs}
        if missing:
            new_str_tag = str_tag
            for k, v in missing.items():
                new_str_tag = new_str_tag.rstrip() + f' {k}="{v}"'
            result = result.replace(f'<{str_tag}>', f'<{new_str_tag}>', 1)
    return result


def fix_language(lang):
    from babel.messages.pofile import read_po, write_po

    po_path = LOCALE_DIR / lang / "messages.po"
    with open(po_path, "rb") as f:
        cat = read_po(f)

    fixed = 0
    for msg in cat:
        if not msg.id or not msg.string:
            continue
        msgid = msg.id if isinstance(msg.id, str) else msg.id[0]
        if isinstance(msg.string, str):
            new_str = fix_attrs(msgid, msg.string)
            if new_str != msg.string:
                msg.string = new_str
                fixed += 1
        elif isinstance(msg.string, tuple):
            new_forms = []
            changed = False
            for form in msg.string:
                if form:
                    new_form = fix_attrs(msgid, form)
                    if new_form != form:
                        changed = True
                    new_forms.append(new_form)
                else:
                    new_forms.append(form)
            if changed:
                msg.string = tuple(new_forms)
                fixed += 1

    with open(po_path, "wb") as f:
        write_po(f, cat, width=10000, omit_header=False)
    print(f"{lang}: fixed {fixed} entries")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: fix_html_attrs.py <lang>")
    fix_language(sys.argv[1])
