"""Tests for the i18n toolbox.

Unit tests use in-memory catalogs (no filesystem, no network).
Integration tests use the real locale/ directory and run CLI subcommands.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest
from babel.messages.pofile import read_po

# Load i18n (no .py extension) as a module — must supply loader explicitly.
from importlib.machinery import SourceFileLoader
_ROOT = Path(__file__).parent.parent
_loader = SourceFileLoader("i18n", str(_ROOT / "i18n"))
_spec = importlib.util.spec_from_file_location("i18n", _ROOT / "i18n", loader=_loader)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

BATCH_SIZE = _mod.BATCH_SIZE
LOCALE_DIR = _mod.LOCALE_DIR
_apply_to_catalog = _mod._apply_to_catalog
_fix_format_errors = _mod._fix_format_errors
_fix_format_type_mismatch = _mod._fix_format_type_mismatch
_fix_header_fuzzy = _mod._fix_header_fuzzy
_fix_html_attrs = _mod._fix_html_attrs
_batches_from_counts = _mod._batches_from_counts
_stats_from_catalog = _mod._stats_from_catalog
_untranslated_from_catalog = _mod._untranslated_from_catalog
known_langs = _mod.known_langs
get_batches = _mod.get_batches
validate = _mod.validate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _catalog(po_bytes: bytes):
    return read_po(BytesIO(po_bytes))


HEADER = b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'


def _po(*entries: str) -> bytes:
    return HEADER + b"\n".join(e.encode() for e in entries) + b"\n"


def _cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_ROOT / "i18n"), *args],
        capture_output=True, text=True, cwd=_ROOT,
    )


# ---------------------------------------------------------------------------
# _stats_from_catalog
# ---------------------------------------------------------------------------

class TestStats:
    def test_counts_correctly(self):
        po = _po(
            'msgid "Hello"\nmsgstr ""',
            'msgid "Goodbye"\nmsgstr "Auf Wiedersehen"',
            '#, fuzzy\nmsgid "Maybe"\nmsgstr "Vielleicht"',
            'msgid "Click %(here)s"\nmsgstr ""',
        )
        s = _stats_from_catalog(_catalog(po), "de")
        assert s["lang"] == "de"
        assert s["translated"] == 1
        assert s["fuzzy"] == 1
        assert s["untranslated"] == 2
        assert s["total"] == 4

    def test_percentage(self):
        po = _po(
            'msgid "A"\nmsgstr "a"',
            'msgid "B"\nmsgstr "b"',
            'msgid "C"\nmsgstr ""',
            'msgid "D"\nmsgstr ""',
        )
        s = _stats_from_catalog(_catalog(po), "xx")
        assert s["pct"] == 50

    def test_empty_catalog(self):
        s = _stats_from_catalog(_catalog(HEADER), "xx")
        assert s["total"] == 0
        assert s["pct"] == 0

    def test_plural_counts_as_translated_when_any_form_filled(self):
        po = _po(
            'msgid "One book"\nmsgid_plural "%(n)d books"\nmsgstr[0] "Ein Buch"\nmsgstr[1] "%(n)d Bücher"',
        )
        s = _stats_from_catalog(_catalog(po), "de")
        assert s["translated"] == 1
        assert s["untranslated"] == 0


# ---------------------------------------------------------------------------
# _untranslated_from_catalog
# ---------------------------------------------------------------------------

class TestUntranslated:
    def test_returns_untranslated_only(self):
        po = _po(
            'msgid "Hello"\nmsgstr ""',
            'msgid "Goodbye"\nmsgstr "Tschüss"',
            '#, fuzzy\nmsgid "Maybe"\nmsgstr "Vielleicht"',
        )
        entries = _untranslated_from_catalog(_catalog(po))
        ids = [e["id"] for e in entries]
        assert "Hello" in ids
        assert "Goodbye" not in ids
        assert "Maybe" not in ids

    def test_limit(self):
        po = _po(
            'msgid "A"\nmsgstr ""',
            'msgid "B"\nmsgstr ""',
            'msgid "C"\nmsgstr ""',
        )
        entries = _untranslated_from_catalog(_catalog(po), limit=2)
        assert len(entries) == 2

    def test_plural_entry_includes_id_plural(self):
        po = _po(
            'msgid "One item"\nmsgid_plural "%(n)d items"\nmsgstr[0] ""\nmsgstr[1] ""',
        )
        entries = _untranslated_from_catalog(_catalog(po))
        assert len(entries) == 1
        assert entries[0]["id"] == "One item"
        assert entries[0]["id_plural"] == "%(n)d items"

    def test_limit_none_returns_all(self):
        po = _po(
            'msgid "A"\nmsgstr ""',
            'msgid "B"\nmsgstr ""',
        )
        entries = _untranslated_from_catalog(_catalog(po), limit=None)
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# _apply_to_catalog
# ---------------------------------------------------------------------------

class TestApply:
    def test_applies_translation(self):
        po = _po('msgid "Hello"\nmsgstr ""')
        cat = _catalog(po)
        count = _apply_to_catalog(cat, {"Hello": "Hallo"})
        assert count == 1
        assert cat["Hello"].string == "Hallo"

    def test_ignores_unknown_keys(self):
        po = _po('msgid "Hello"\nmsgstr ""')
        cat = _catalog(po)
        assert _apply_to_catalog(cat, {"Nonexistent": "X"}) == 0

    def test_plural_applies_list(self):
        po = _po(
            'msgid "One item"\nmsgid_plural "%(n)d items"\nmsgstr[0] ""\nmsgstr[1] ""',
        )
        cat = _catalog(po)
        count = _apply_to_catalog(cat, {"One item": ["Ein Eintrag", "%(n)d Einträge"]})
        assert count == 1
        assert cat[("One item", "%(n)d items")].string == ("Ein Eintrag", "%(n)d Einträge")

    def test_overwrites_existing(self):
        po = _po('msgid "Hello"\nmsgstr "Existing"')
        cat = _catalog(po)
        _apply_to_catalog(cat, {"Hello": "New"})
        assert cat["Hello"].string == "New"


# ---------------------------------------------------------------------------
# _fix_html_attrs
# ---------------------------------------------------------------------------

class TestFixHtmlAttrs:
    def test_adds_missing_rel_attribute(self):
        po = _po(
            "msgid \"Click <a href='/x' rel='noopener'>here</a>\"\n"
            "msgstr \"Klicken <a href='/x'>hier</a>\"",
        )
        cat = _catalog(po)
        fixed = _fix_html_attrs(cat)
        assert fixed == 1
        msgstr = cat["Click <a href='/x' rel='noopener'>here</a>"].string
        assert "rel=" in msgstr and "noopener" in msgstr

    def test_no_change_when_attrs_match(self):
        po = _po(
            "msgid \"See <a href='/x'>here</a>\"\n"
            "msgstr \"Voir <a href='/x'>ici</a>\"",
        )
        assert _fix_html_attrs(_catalog(po)) == 0

    def test_skips_when_tag_count_differs(self):
        po = _po(
            "msgid \"<a href='/'>A</a> and <b>B</b>\"\n"
            "msgstr \"<a href='/'>A</a>\"",
        )
        assert _fix_html_attrs(_catalog(po)) == 0

    def test_skips_empty_msgstr(self):
        po = _po(
            "msgid \"See <a rel='noopener'>here</a>\"\n"
            "msgstr \"\"",
        )
        assert _fix_html_attrs(_catalog(po)) == 0


# ---------------------------------------------------------------------------
# _fix_format_errors
# ---------------------------------------------------------------------------

class TestFixFormatErrors:
    def test_clears_named_placeholder_mismatch(self):
        po = _po('msgid "Hello %(name)s"\nmsgstr "Hallo %(wrong)s"')
        cat = _catalog(po)
        cleared = _fix_format_errors(cat)
        assert len(cleared) == 1
        assert cat["Hello %(name)s"].string == ""

    def test_clears_extra_placeholder_in_msgstr(self):
        po = _po('msgid "Hello"\nmsgstr "Hallo %(name)s"')
        assert len(_fix_format_errors(_catalog(po))) == 1

    def test_no_change_when_placeholders_match(self):
        po = _po('msgid "Hello %(name)s"\nmsgstr "Hallo %(name)s"')
        assert _fix_format_errors(_catalog(po)) == []

    def test_clears_positional_count_mismatch(self):
        po = _po('msgid "Page %s of %s"\nmsgstr "Seite %s"')
        assert len(_fix_format_errors(_catalog(po))) == 1

    def test_no_change_when_no_placeholders(self):
        po = _po('msgid "Hello"\nmsgstr "Hallo"')
        assert _fix_format_errors(_catalog(po)) == []

    def test_returns_cleared_msgids(self):
        po = _po('msgid "Hello %(name)s"\nmsgstr "Hallo %(wrong)s"')
        cat = _catalog(po)
        cleared = _fix_format_errors(cat)
        assert isinstance(cleared, list)
        assert "Hello %(name)s" in cleared

    def test_returns_empty_list_when_no_errors(self):
        po = _po('msgid "Hello %(name)s"\nmsgstr "Hallo %(name)s"')
        assert _fix_format_errors(_catalog(po)) == []


class TestFixFormatErrorsPluralPlaceholders:
    """A form the plural rule selects for some n other than 1 may carry the count (#15).

    With nplurals=1 that is msgstr[0] at every n; in ru/uk/hr it is msgstr[0]
    at n=21, 31, ...
    """

    HEADER_1 = (
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        b'"Plural-Forms: nplurals=1; plural=0;\\n"\n\n'
    )
    HEADER_2 = (
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        b'"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
    )
    HEADER_RU = (
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        b'"Plural-Forms: nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : '
        b'n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);\\n"\n\n'
    )
    HEADER_PL = (
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        b'"Plural-Forms: nplurals=3; plural=(n==1 ? 0 : '
        b'n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);\\n"\n\n'
    )
    ENTRY = (
        'msgid "There is one person ahead of you."\n'
        'msgid_plural "There are %(count)d people ahead of you."\n'
        'msgstr[0] "{0}"\n'
    )

    def _cat(self, header: bytes, msgstr0: str, extra: str = ""):
        return _catalog(header + (self.ENTRY.format(msgstr0) + extra).encode())

    def test_keeps_plural_placeholder_in_single_form(self):
        cat = self._cat(self.HEADER_1, "あなたの前に%(count)d人います。")
        assert _fix_format_errors(cat) == []
        assert cat["There is one person ahead of you."].string == ("あなたの前に%(count)d人います。",)

    def test_keeps_positional_plural_placeholder_in_single_form(self):
        po = self.HEADER_1 + b'msgid "one item"\nmsgid_plural "%d items"\nmsgstr[0] "%d\xe4\xbb\xb6"\n'
        assert _fix_format_errors(_catalog(po)) == []

    def test_still_clears_unknown_placeholder_in_single_form(self):
        cat = self._cat(self.HEADER_1, "あなたの前に%(wrong)d人います。")
        assert _fix_format_errors(cat) == ["There is one person ahead of you."]

    def test_still_clears_single_form_missing_singular_placeholder(self):
        po = self.HEADER_1 + (
            'msgid "%(who)s merged one duplicate"\n'
            'msgid_plural "%(who)s merged %(count)d duplicates"\n'
            'msgstr[0] "%(count)d件を統合しました"\n'
        ).encode()
        assert _fix_format_errors(_catalog(po)) == ["%(who)s merged one duplicate"]

    def test_keeps_plural_placeholder_in_form_also_selected_by_21(self):
        cat = self._cat(self.HEADER_RU, "Перед вами %(count)d человек.",
                        'msgstr[1] "Перед вами %(count)d человека."\n'
                        'msgstr[2] "Перед вами %(count)d человек."\n')
        assert _fix_format_errors(cat) == []

    def test_still_clears_unknown_placeholder_in_form_also_selected_by_21(self):
        cat = self._cat(self.HEADER_RU, "Перед вами %(wrong)d человек.",
                        'msgstr[1] "Перед вами %(count)d человека."\n'
                        'msgstr[2] "Перед вами %(count)d человек."\n')
        assert _fix_format_errors(cat) == ["There is one person ahead of you."]

    def test_form_selected_only_by_one_still_checked_against_msgid(self):
        cat = self._cat(self.HEADER_2, "Vor Ihnen sind %(count)d Personen.",
                        'msgstr[1] "Vor Ihnen sind %(count)d Personen."\n')
        assert _fix_format_errors(cat) == ["There is one person ahead of you."]

    def test_three_form_rule_selecting_form_0_only_at_one_still_checked(self):
        cat = self._cat(self.HEADER_PL, "Przed tobą jest %(count)d osoba.",
                        'msgstr[1] "Przed tobą są %(count)d osoby."\n'
                        'msgstr[2] "Przed tobą jest %(count)d osób."\n')
        assert _fix_format_errors(cat) == ["There is one person ahead of you."]

    def test_non_plural_message_in_single_form_catalog_still_checked_against_msgid(self):
        po = self.HEADER_1 + '#, python-format\nmsgid "%s works"\nmsgstr "作品"\n'.encode()
        assert _fix_format_errors(_catalog(po)) == ["%s works"]

    def test_non_plural_message_dropping_named_placeholder_still_cleared(self):
        po = _po('msgid "Hello %(name)s"\nmsgstr "Hallo"')
        assert _fix_format_errors(_catalog(po)) == ["Hello %(name)s"]

    @pytest.mark.parametrize("rule", ["n ! = 1", "(1/n)", "(n%0)"])
    def test_malformed_plural_rule_does_not_crash_and_keeps_old_behaviour(self, rule):
        header = (
            b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
            b'"Plural-Forms: nplurals=2; plural=' + rule.encode() + b';\\n"\n\n'
        )
        cat = self._cat(header, "Vor Ihnen sind %(count)d Personen.",
                        'msgstr[1] "Vor Ihnen sind %(count)d Personen."\n')
        assert _fix_format_errors(cat) == ["There is one person ahead of you."]


# ---------------------------------------------------------------------------
# _fix_format_type_mismatch
# ---------------------------------------------------------------------------

class TestFixFormatTypeMismatch:
    def test_fixes_d_to_s_in_plural_form(self):
        po = _po(
            'msgid "One item"\n'
            'msgid_plural "%(n)s items"\n'
            'msgstr[0] "Un élément"\n'
            'msgstr[1] "%(n)d éléments"',
        )
        cat = _catalog(po)
        fixed = _fix_format_type_mismatch(cat)
        assert len(fixed) == 1
        msgstr = cat[("One item", "%(n)s items")].string
        assert "%(n)d" not in (msgstr[1] or "")
        assert "%(n)s" in (msgstr[1] or "")

    def test_no_change_when_types_match(self):
        po = _po('msgid "Hello %(name)s"\nmsgstr "Hallo %(name)s"')
        assert _fix_format_type_mismatch(_catalog(po)) == []

    def test_no_change_when_no_named_placeholders(self):
        po = _po('msgid "Page %s of %s"\nmsgstr "Seite %s von %s"')
        assert _fix_format_type_mismatch(_catalog(po)) == []

    def test_fixes_invalid_format_specifier_in_msgstr(self):
        # %(count)개 is not a valid Python format spec — %(count)s is in msgid
        po = _po('msgid "%(count)s commits"\nmsgstr "%(count)개 커밋"')
        cat = _catalog(po)
        # _fix_format_errors should clear this (%(count) not matchable) not type-fix
        # so _fix_format_type_mismatch doesn't apply here — counts stay as-is
        # (this case is caught by _fix_format_errors name mismatch, not type mismatch)
        result = _fix_format_type_mismatch(cat)
        assert result == []  # type mismatch only fixes valid→valid type change


# ---------------------------------------------------------------------------
# _fix_header_fuzzy
# ---------------------------------------------------------------------------

class TestFixHeaderFuzzy:
    def test_strips_fuzzy_from_catalog_header(self):
        from babel.messages.pofile import read_po
        po = b'#, fuzzy\nmsgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        cat = read_po(BytesIO(po))
        assert cat.fuzzy
        count = _fix_header_fuzzy(cat)
        assert count == 1
        assert not cat.fuzzy

    def test_no_change_when_header_not_fuzzy(self):
        cat = _catalog(HEADER)
        assert _fix_header_fuzzy(cat) == 0

    def test_does_not_touch_content_entry_fuzzy_flags(self):
        po = _po('#, fuzzy\nmsgid "Hello"\nmsgstr "Hallo"')
        cat = _catalog(po)
        _fix_header_fuzzy(cat)
        assert cat["Hello"].fuzzy  # content entry untouched


# ---------------------------------------------------------------------------
# _fix_validator_failures — catch-all: clear entries still failing after other fixes
# ---------------------------------------------------------------------------

class TestFixValidatorFailures:
    def test_clears_entry_that_fails_babel_check(self):
        # %(n)s in msgid_plural but msgstr[1] uses %(n)d — babel catches type mismatch
        po = _po(
            'msgid "One item"\n'
            'msgid_plural "%(n)s items"\n'
            'msgstr[0] "Ein Element"\n'
            'msgstr[1] "%(n)d Elemente"',
        )
        cat = _catalog(po)
        cleared = _mod._fix_validator_failures(cat)
        assert len(cleared) == 1
        # msgstr should be cleared
        msg = cat[("One item", "%(n)s items")]
        assert not any(msg.string)

    def test_no_change_for_valid_entries(self):
        po = _po('msgid "Hello %(name)s"\nmsgstr "Hallo %(name)s"')
        assert _mod._fix_validator_failures(_catalog(po)) == []


class TestValidateCfmtOrder:
    """Named placeholders are looked up by key, so a translation may reorder or
    repeat them; positional ones bind in sequence, so their order must hold."""

    def _errors(self, msgid: str, msgstr: str):
        po = _po(f'#, python-format\nmsgid "{msgid}"\nmsgstr "{msgstr}"')
        cat = _catalog(po)
        return _mod._get_validate_fn()(cat[msgid], cat)

    def test_accepts_reordered_named_placeholders(self):
        # openlibrary's uk translation, excluded from the port (#105) by this bug.
        msgid = ("%(username)s has read %(total)d books. Join %(username)s on "
                 "OpenLibrary.org and tell the world about the books that you care about.")
        msgstr = ("%(total)d книг були прочитані %(username)s. Приєднайтеся до %(username)s "
                  "на OpenLibrary.org і розкажіть світові про книги, які вас хвилюють.")
        assert self._errors(msgid, msgstr) == []

    def test_still_rejects_reordered_positional_placeholders(self):
        assert self._errors("Page %s of %d", "%d Seite von %s") != []

    def test_fingerprint_keeps_positional_order(self):
        # Babel's own check also rejects the case above, so pin the fingerprint itself.
        fingerprint = _mod._get_validate_fn().__globals__["_cfmt_fingerprint"]
        assert fingerprint("Page %s of %d") != fingerprint("%d Seite von %s")

    def test_still_rejects_dropped_named_placeholder(self):
        assert self._errors("%(a)s and %(b)s", "%(a)s und") != []

    def test_still_rejects_unknown_named_placeholder(self):
        assert self._errors("%(a)s and %(b)s", "%(a)s und %(c)s") != []

    def test_still_rejects_dropped_percent_literal(self):
        assert self._errors("100%% Complete!", "100 Fertig!") != []

    def test_accepts_named_placeholder_repeated_fewer_times(self):
        assert self._errors("%(a)s and %(a)s", "%(a)s") == []


class TestValidatePluralPlaceholders:
    """A form selected for some n other than 1 answers to msgid_plural (#15)."""

    HEADER_1 = TestFixFormatErrorsPluralPlaceholders.HEADER_1
    HEADER_2 = TestFixFormatErrorsPluralPlaceholders.HEADER_2
    HEADER_RU = TestFixFormatErrorsPluralPlaceholders.HEADER_RU
    HEADER_PL = TestFixFormatErrorsPluralPlaceholders.HEADER_PL
    RU_REST = 'msgstr[1] "У %(name)s %(count)s списка."\nmsgstr[2] "У %(name)s %(count)s списков."\n'
    ENTRY = (
        'msgid "%(name)s has 1 list."\n'
        'msgid_plural "%(name)s has %(count)s lists."\n'
        'msgstr[0] "{0}"\n'
    )

    def _errors(self, header: bytes, msgstr0: str, extra: str = ""):
        cat = _catalog(header + (self.ENTRY.format(msgstr0) + extra).encode())
        return _mod._get_validate_fn()(cat[("%(name)s has 1 list.", "%(name)s has %(count)s lists.")], cat)

    def test_accepts_plural_placeholder_in_single_form(self):
        assert self._errors(self.HEADER_1, "%(name)sには%(count)s件のリストがあります。") == []

    def test_fix_keeps_plural_placeholder_in_single_form(self):
        po = self.HEADER_1 + self.ENTRY.format("%(name)sには%(count)s件のリストがあります。").encode()
        assert _mod._fix_validator_failures(_catalog(po)) == []

    def test_still_rejects_unknown_placeholder_in_single_form(self):
        assert self._errors(self.HEADER_1, "%(name)sには%(wrong)s件のリストがあります。") != []

    def test_still_rejects_type_mismatch_in_single_form(self):
        assert self._errors(self.HEADER_1, "%(name)sには%(count)d件のリストがあります。") != []

    def test_accepts_plural_placeholder_in_form_also_selected_by_21(self):
        assert self._errors(self.HEADER_RU, "У %(name)s %(count)s список.", self.RU_REST) == []

    def test_still_rejects_unknown_placeholder_in_form_also_selected_by_21(self):
        assert self._errors(self.HEADER_RU, "У %(name)s %(wrong)s список.", self.RU_REST) != []

    def test_form_selected_only_by_one_still_checked_against_msgid(self):
        extra = 'msgstr[1] "%(name)s hat %(count)s Listen."\n'
        assert self._errors(self.HEADER_2, "%(name)s hat %(count)s Liste.", extra) != []

    def test_three_form_rule_selecting_form_0_only_at_one_still_checked(self):
        extra = 'msgstr[1] "%(name)s ma %(count)s listy."\nmsgstr[2] "%(name)s ma %(count)s list."\n'
        assert self._errors(self.HEADER_PL, "%(name)s ma %(count)s listę.", extra) != []

    def test_rejects_unknown_placeholder_when_plural_msgid_has_none(self):
        po = self.HEADER_1 + (
            '#, python-format\nmsgid "%(name)s has one list"\nmsgid_plural "Many lists"\n'
            'msgstr[0] "%(bogus)s"\n'
        ).encode()
        cat = _catalog(po)
        assert _mod._get_validate_fn()(cat[("%(name)s has one list", "Many lists")], cat) != []

    def test_rejects_placeholder_neither_msgid_has_in_form_shown_at_zero(self):
        header = (
            b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
            b'"Plural-Forms: nplurals=2; plural=(n > 1);\\n"\n\n'
        )
        po = header + (
            '#, python-format\nmsgid "%(name)s has one list"\nmsgid_plural "Lists"\n'
            'msgstr[0] "%(name)s a %(count)d liste"\nmsgstr[1] "Des listes"\n'
        ).encode()
        cat = _catalog(po)
        assert _mod._get_validate_fn()(cat[("%(name)s has one list", "Lists")], cat) != []

    def test_rejects_type_mismatch_when_plural_msgid_brings_nothing(self):
        # A placeholder-free msgid_plural must not switch Babel's singular check off.
        po = self.HEADER_1 + (
            '#, python-format\nmsgid "%(name)s has one list"\nmsgid_plural "Lists"\n'
            'msgstr[0] "%(name)d件"\n'
        ).encode()
        cat = _catalog(po)
        assert _mod._get_validate_fn()(cat[("%(name)s has one list", "Lists")], cat) != []

    def test_plural_msgid_with_fewer_positionals_brings_nothing(self):
        po = self.HEADER_1 + (
            '#, python-format\nmsgid "%s of %d"\nmsgid_plural "%s"\nmsgstr[0] "%s"\n'
        ).encode()
        cat = _catalog(po)
        assert _mod._get_validate_fn()(cat[("%s of %d", "%s")], cat) != []

    def test_mixed_kind_msgids_get_no_plural_allowance_in_validate_or_fix(self):
        po = self.HEADER_1 + (
            '#, python-format\nmsgid "one %s"\nmsgid_plural "%(count)d of them"\n'
            'msgstr[0] "それら"\n'
        ).encode()
        cat = _catalog(po)
        assert _mod._get_validate_fn()(cat[("one %s", "%(count)d of them")], cat) != []
        assert _fix_format_errors(_catalog(po)) == ["one %s"]

    def test_still_requires_singular_placeholder_like_fix_does(self):
        # `./i18n fix` clears this (see TestFixFormatErrorsPluralPlaceholders); validate must agree.
        po = self.HEADER_1 + (
            '#, python-format\nmsgid "%(who)s merged one duplicate"\n'
            'msgid_plural "%(who)s merged %(count)d duplicates"\nmsgstr[0] "%(count)d件"\n'
        ).encode()
        cat = _catalog(po)
        key = ("%(who)s merged one duplicate", "%(who)s merged %(count)d duplicates")
        assert _mod._get_validate_fn()(cat[key], cat) != []
        assert _fix_format_errors(_catalog(po)) == ["%(who)s merged one duplicate"]

    @pytest.mark.parametrize("rule", ["n ! = 1", "(1/n)", "(n%0)"])
    def test_malformed_plural_rule_does_not_crash(self, rule):
        header = (
            b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
            b'"Plural-Forms: nplurals=2; plural=' + rule.encode() + b';\\n"\n\n'
        )
        extra = 'msgstr[1] "%(name)s hat %(count)s Listen."\n'
        assert self._errors(header, "%(name)s hat %(count)s Liste.", extra) != []

    def test_non_plural_message_still_checked_against_msgid(self):
        cat = _catalog(self.HEADER_1 + 'msgid "Hello %(name)s"\nmsgstr "%(name)s %(count)s"\n'.encode())
        errors = _mod._get_validate_fn()(cat["Hello %(name)s"], cat)
        # Babel's own check, not only the custom cfmt one that also fires here.
        assert any("unknown named placeholder 'count'" in e for e in errors)


# ---------------------------------------------------------------------------
# _batches_from_counts (pure — the key orchestration function)
# ---------------------------------------------------------------------------

class TestBatchesFromCounts:
    def _counts(self, items: list[tuple[str, int]]) -> list[dict]:
        return [{"lang": lang, "untranslated": n} for lang, n in items]

    def test_small_langs_grouped_into_one_batch(self):
        counts = self._counts([("de", 30), ("fr", 50), ("es", 75)])
        batches = _batches_from_counts(counts, batch_size=75)
        assert len(batches) == 1
        assert set(batches[0]) == {"de", "fr", "es"}

    def test_large_langs_each_get_own_batch(self):
        counts = self._counts([("de", 100), ("fr", 200)])
        batches = _batches_from_counts(counts, batch_size=75)
        assert len(batches) == 2
        assert {"de"} in [set(b) for b in batches]
        assert {"fr"} in [set(b) for b in batches]

    def test_mixed_small_and_large(self):
        counts = self._counts([("de", 30), ("fr", 200), ("es", 75), ("ja", 300)])
        batches = _batches_from_counts(counts, batch_size=75)
        assert len(batches) == 3
        shared = next(b for b in batches if len(b) > 1)
        assert set(shared) == {"de", "es"}
        solos = [b[0] for b in batches if len(b) == 1]
        assert set(solos) == {"fr", "ja"}

    def test_zero_untranslated_excluded(self):
        counts = self._counts([("de", 0), ("fr", 0)])
        assert _batches_from_counts(counts) == []

    def test_empty_counts(self):
        assert _batches_from_counts([]) == []

    def test_exactly_at_batch_size_goes_to_shared(self):
        counts = self._counts([("de", BATCH_SIZE)])
        batches = _batches_from_counts(counts)
        assert len(batches) == 1
        assert batches[0] == ["de"]

    def test_one_over_batch_size_gets_own_batch(self):
        counts = self._counts([("de", BATCH_SIZE + 1)])
        batches = _batches_from_counts(counts)
        assert len(batches) == 1
        assert batches[0] == ["de"]

    def test_no_lang_in_multiple_batches(self):
        counts = self._counts([("de", 30), ("fr", 200), ("es", 50), ("ja", 0)])
        batches = _batches_from_counts(counts)
        all_langs = [lang for batch in batches for lang in batch]
        assert len(all_langs) == len(set(all_langs))

    def test_custom_batch_size(self):
        counts = self._counts([("de", 10), ("fr", 20)])
        batches = _batches_from_counts(counts, batch_size=15)
        assert len(batches) == 2


# ---------------------------------------------------------------------------
# Integration: CLI against real locale/
# ---------------------------------------------------------------------------

class TestCLI:
    def test_incomplete_returns_valid_json(self):
        r = _cli("incomplete")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        for item in data:
            assert "lang" in item and "untranslated" in item
            assert item["untranslated"] > 0

    def test_incomplete_sorted_descending(self):
        r = _cli("incomplete")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        counts = [item["untranslated"] for item in data]
        assert counts == sorted(counts, reverse=True)

    def test_batch_returns_valid_json(self):
        r = _cli("batch")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert "batches" in data
        assert isinstance(data["batches"], list)
        for batch in data["batches"]:
            assert isinstance(batch, list)
            assert len(batch) >= 1

    def test_batch_no_lang_in_multiple_batches(self):
        r = _cli("batch")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        all_langs = [lang for batch in data["batches"] for lang in batch]
        assert len(all_langs) == len(set(all_langs))

    def test_batch_langs_are_subset_of_known(self):
        r = _cli("batch")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        all_langs = {lang for batch in data["batches"] for lang in batch}
        assert all_langs <= set(known_langs())

    def test_untranslated_returns_valid_json(self):
        lang = known_langs()[0]
        r = _cli("untranslated", lang, "--limit", "1")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        if data:
            assert "id" in data[0]

    def test_validate_runs(self):
        lang = known_langs()[0]
        r = _cli("validate", lang)
        assert r.returncode == 0, r.stderr

    def test_sync_suppresses_pybabel_stderr(self):
        """sync must suppress pybabel's 'updating catalog...' noise on stderr."""
        lang = known_langs()[0]
        r = _cli("sync", lang)
        assert r.returncode == 0, r.stderr
        # pybabel writes "updating catalog..." to stderr; we suppress it with DEVNULL
        assert "updating catalog" not in r.stderr


# ---------------------------------------------------------------------------
# validate() — sys.path must not be mutated
# ---------------------------------------------------------------------------

class TestValidateSysPath:
    def test_does_not_mutate_sys_path(self):
        """validate() must not permanently insert TESTS_DIR into sys.path."""
        path_before = list(sys.path)
        lang = known_langs()[0]
        validate(lang)
        assert sys.path == path_before, (
            "validate() permanently modified sys.path via sys.path.insert(); "
            "use importlib instead"
        )
