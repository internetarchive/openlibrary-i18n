"""scripts/po_same_content.py"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "po_same_content.py"
_spec = importlib.util.spec_from_file_location("po_same_content", SCRIPT)
psc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(psc)

BASE = '''msgid ""
msgstr ""
"PO-Revision-Date: 2026-11-01 13:33+0000\\n"
"Last-Translator: Someone <someone@example.com>\\n"
"Language: fr\\n"
"Content-Type: text/plain; charset=utf-8\\n"

#: admin/ip/view.html
#, python-format
msgid "Block %(ip)s"
msgstr "Bloquer %(ip)s"

msgid "Settings"
msgstr "Paramètres"

#~ msgid "Batch ID:"
#~ msgstr "ID de lot :"
'''


def same(tmp_path, a: str, b: str) -> bool:
    pa, pb = tmp_path / "a.po", tmp_path / "b.po"
    pa.write_text(a, encoding="utf-8")
    pb.write_text(b, encoding="utf-8")
    return psc.main([str(pa), str(pb)]) == 0


def test_header_and_trailing_newline_ignored(tmp_path):
    # PR #287: Weblate's file differed from the healed one only like this.
    weblate = BASE.replace("2026-11-01 13:33", "2026-11-16 23:18").replace(
        "Someone <someone@example.com>", "admin <admin@example.com>"
    ).replace(
        '"Content-Type: text/plain; charset=utf-8\\n"\n',
        '"Content-Type: text/plain; charset=utf-8\\n"\n"X-Generator: Weblate 2026.11.2\\n"\n',
    ).rstrip("\n")
    assert same(tmp_path, BASE, weblate)


def test_locations_and_wrapping_ignored(tmp_path):
    other = BASE.replace("#: admin/ip/view.html\n", "#: admin/ip/view.html admin/x.html\n").replace(
        'msgstr "Paramètres"', 'msgstr ""\n"Param"\n"ètres"'
    )
    assert same(tmp_path, BASE, other)


def test_changed_translation_differs(tmp_path):
    assert not same(tmp_path, BASE, BASE.replace('"Paramètres"', '"Réglages"'))


def test_fuzzy_flag_differs(tmp_path):
    assert not same(tmp_path, BASE, BASE.replace("#, python-format", "#, fuzzy, python-format"))


def test_added_message_differs(tmp_path):
    assert not same(tmp_path, BASE, BASE + '\nmsgid "New"\nmsgstr "Nouveau"\n')


def test_obsolete_entry_differs(tmp_path):
    assert not same(tmp_path, BASE, BASE.replace('"ID de lot :"', '"Lot :"'))
