"""Tests for the i18n toolbox.

Unit tests use in-memory catalogs (no filesystem, no network).
Integration tests use the real locale/ directory and run CLI subcommands.
"""
from __future__ import annotations

import importlib.util
import json
import os
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
_overlay_translations = _mod._overlay_translations
_is_translated = _mod._is_translated
_fix_format_errors = _mod._fix_format_errors
_fix_format_type_mismatch = _mod._fix_format_type_mismatch
_fix_header_fuzzy = _mod._fix_header_fuzzy
_fix_html_attrs = _mod._fix_html_attrs
_fix_plural_placeholder_defects = _mod._fix_plural_placeholder_defects
_repair_plural_form = _mod._repair_plural_form
_did_translation_work = _mod._did_translation_work
_as_tuple = _mod._as_tuple
check_scope = _mod.check_scope
isolate = _mod.isolate
_restore_live_in_catalog = _mod._restore_live_in_catalog
_english_copy_or_empty = _mod._english_copy_or_empty
_batches_from_counts = _mod._batches_from_counts
_stats_from_catalog = _mod._stats_from_catalog
_untranslated_from_catalog = _mod._untranslated_from_catalog
_add_missing_from_pot = _mod._add_missing_from_pot
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
        # The fuzzy entry is shown in English, so it is untranslated work (#334).
        assert s["untranslated"] == 3
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
        assert "Maybe" in ids  # fuzzy: needs a real translation (#334)

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


class TestMissingFromPot:
    """msgids in messages.pot but absent from the .po must count as untranslated."""

    POT = _po(
        'msgid "Have"\nmsgstr ""',
        'msgid "New"\nmsgstr ""',
        'msgid "One new"\nmsgid_plural "%(n)d new"\nmsgstr[0] ""\nmsgstr[1] ""',
    )
    PO = _po('msgid "Have"\nmsgstr "Hat"')

    def test_add_missing_from_pot(self):
        cat = _catalog(self.PO)
        assert _add_missing_from_pot(cat, _catalog(self.POT)) == 2
        ids = [e["id"] for e in _untranslated_from_catalog(cat)]
        assert ids == ["New", "One new"]
        assert _stats_from_catalog(cat, "xx")["untranslated"] == 2

    @pytest.fixture
    def fake_repo(self, tmp_path, monkeypatch):
        (tmp_path / "locale" / "xx").mkdir(parents=True)
        (tmp_path / "locale" / "xx" / "messages.po").write_bytes(self.PO)
        (tmp_path / "messages.pot").write_bytes(self.POT)
        monkeypatch.setattr(_mod, "LOCALE_DIR", tmp_path / "locale")
        monkeypatch.setattr(_mod, "MESSAGES_POT", tmp_path / "messages.pot")
        return tmp_path

    def test_get_untranslated_and_counts_see_missing(self, fake_repo):
        assert len(_mod.get_untranslated("xx")) == 2
        assert _mod.get_counts("xx")["untranslated"] == 2

    def test_apply_adds_missing_entries(self, fake_repo):
        assert _mod.apply_translations("xx", {"New": "Neu"}) == 1
        cat = read_po(open(fake_repo / "locale" / "xx" / "messages.po", "rb"))
        assert cat["New"].string == "Neu"
        assert cat["Have"].string == "Hat"
        assert [e["id"] for e in _mod.get_untranslated("xx")] == ["One new"]


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
# _overlay_translations — the auto-heal core (resolve)
# ---------------------------------------------------------------------------

class TestOverlayTranslations:
    """The merge that heals a conflicting PR: overlay incoming's translations
    onto base, incoming-wins, NEVER losing a base translation or adding a string
    outside base's template."""

    def test_incoming_wins_when_both_translated(self):
        base = _catalog(_po('msgid "Hi"\nmsgstr "Old"'))
        incoming = _catalog(_po('msgid "Hi"\nmsgstr "New"'))
        stats = _overlay_translations(base, incoming)
        assert base["Hi"].string == "New"
        assert stats["changed"] == 1 and stats["filled"] == 0

    def test_incoming_fills_an_empty_base_string(self):
        base = _catalog(_po('msgid "Hi"\nmsgstr ""'))
        incoming = _catalog(_po('msgid "Hi"\nmsgstr "Bonjour"'))
        stats = _overlay_translations(base, incoming)
        assert base["Hi"].string == "Bonjour"
        assert stats["filled"] == 1 and stats["after"] == 1 and stats["before"] == 0

    def test_empty_incoming_never_drops_a_base_translation(self):
        # The whole safety property: an empty/absent incoming msgstr keeps base's.
        base = _catalog(_po('msgid "Hi"\nmsgstr "Kept"'))
        incoming = _catalog(_po('msgid "Hi"\nmsgstr ""'))
        stats = _overlay_translations(base, incoming)
        assert base["Hi"].string == "Kept"
        assert stats["changed"] == 0 and stats["after"] == stats["before"] == 1

    def test_incoming_only_msgid_is_ignored_no_scope_creep(self):
        base = _catalog(_po('msgid "Hi"\nmsgstr "Hola"'))
        incoming = _catalog(
            _po('msgid "Hi"\nmsgstr "Hola"', 'msgid "Stray"\nmsgstr "Nope"')
        )
        _overlay_translations(base, incoming)
        assert "Stray" not in base  # base's template is the floor; no new strings

    def test_plurals_overlay_as_a_unit(self):
        base = _catalog(_po(
            'msgid "One"\nmsgid_plural "%(n)d"\nmsgstr[0] ""\nmsgstr[1] ""'))
        incoming = _catalog(_po(
            'msgid "One"\nmsgid_plural "%(n)d"\nmsgstr[0] "Un"\nmsgstr[1] "%(n)d"'))
        _overlay_translations(base, incoming)
        assert base[("One", "%(n)d")].string == ("Un", "%(n)d")

    def test_net_translated_count_never_decreases(self):
        # Base has 2 translated; incoming would blank one and change another.
        # The blank must be ignored, so after >= before.
        base = _catalog(_po(
            'msgid "A"\nmsgstr "a"', 'msgid "B"\nmsgstr "b"'))
        incoming = _catalog(_po(
            'msgid "A"\nmsgstr ""', 'msgid "B"\nmsgstr "b2"'))
        stats = _overlay_translations(base, incoming)
        assert stats["after"] >= stats["before"] == 2
        assert base["A"].string == "a" and base["B"].string == "b2"

    def test_overlay_adopts_contributor_clean_review_state(self):
        # Base's AI translation was fuzzy (stale/uncertain). The contributor
        # submitted a clean (reviewed) correction. The overlay must adopt the
        # contributor's review state so the correction goes LIVE (clean entries
        # compile into the .mo; fuzzy ones do not). Keeping base's fuzzy flag
        # would systematically land contributor corrections as non-live.
        base = _catalog(_po('#, fuzzy\nmsgid "Borrows"\nmsgstr "Emprunter"'))
        incoming = _catalog(_po('msgid "Borrows"\nmsgstr "Emprunts"'))
        _overlay_translations(base, incoming)
        assert base["Borrows"].string == "Emprunts"
        assert not base["Borrows"].fuzzy  # contributor's clean state adopted

    def test_overlay_preserves_contributor_fuzzy_state(self):
        # Converse: the contributor left their own translation fuzzy (needs
        # work). Even if base was clean, adopt the contributor's fuzzy state so
        # we never present their unfinished work as reviewed/live.
        base = _catalog(_po('msgid "Hi"\nmsgstr "Salut"'))
        incoming = _catalog(_po('#, fuzzy\nmsgid "Hi"\nmsgstr "Coucou"'))
        _overlay_translations(base, incoming)
        assert base["Hi"].string == "Coucou"
        assert base["Hi"].fuzzy  # contributor's fuzzy state preserved

    def test_is_translated_handles_plural_tuples(self):
        assert _is_translated("x") is True
        assert _is_translated("") is False
        assert _is_translated(("", "")) is False
        assert _is_translated(("", "y")) is True


class TestDeRegress:
    """de-regress restores openlibrary's LIVE translation wherever baking this
    catalog would drop a string back to English — the bake-regression gating class
    that `fix` cannot repair. It must restore empty/english-copy msgstrs and never
    overwrite a contributor's genuine translation."""

    @staticmethod
    def _finding(lang, msgid, live, msgctxt=None):
        return {"kind": "regression", "gating": True, "lang": lang,
                "msgid": msgid, "msgctxt": msgctxt, "openlibrary_msgstr": live}

    def test_restores_english_copy_to_live(self):
        # The exact #212 case: sc 'LEADER:' shipped as an english-copy would
        # regress openlibrary's live 'DIRETORE:' to English. de-regress restores it.
        cat = _catalog(_po('msgid "LEADER:"\nmsgstr "LEADER:"'))
        restored, skipped = _restore_live_in_catalog(
            cat, [self._finding("sc", "LEADER:", "DIRETORE:")])
        assert restored == 1 and skipped == 0
        assert cat["LEADER:"].string == "DIRETORE:"

    def test_restores_empty_to_live(self):
        cat = _catalog(_po('msgid "LibriVox"\nmsgstr ""'))
        restored, _ = _restore_live_in_catalog(
            cat, [self._finding("sc", "LibriVox", "Librivox")])
        assert restored == 1
        assert cat["LibriVox"].string == "Librivox"

    def test_never_overwrites_a_genuine_translation(self):
        # The safety invariant: if the catalog already carries a real translation,
        # de-regress leaves it ALONE even if openlibrary's live value differs. A
        # contributor's deliberate work is never clobbered. (Mutating the
        # _english_copy_or_empty guard to always-True makes this test fail — that is
        # the line it pins.)
        cat = _catalog(_po('msgid "Hi"\nmsgstr "Salut"'))
        restored, skipped = _restore_live_in_catalog(
            cat, [self._finding("fr", "Hi", "Bonjour")])
        assert restored == 0 and skipped == 1
        assert cat["Hi"].string == "Salut"  # contributor's translation preserved

    def test_restore_clears_stale_fuzzy_flag(self):
        cat = _catalog(_po('#, fuzzy\nmsgid "LEADER:"\nmsgstr "LEADER:"'))
        _restore_live_in_catalog(cat, [self._finding("sc", "LEADER:", "DIRETORE:")])
        assert cat["LEADER:"].string == "DIRETORE:"
        assert not cat["LEADER:"].fuzzy

    def test_ignores_non_gating_and_non_regression(self):
        cat = _catalog(_po('msgid "LEADER:"\nmsgstr "LEADER:"'))
        findings = [
            {"kind": "regression", "gating": False, "lang": "sc",
             "msgid": "LEADER:", "msgctxt": None, "openlibrary_msgstr": "DIRETORE:"},
            {"kind": "new_locale", "gating": True, "lang": "sc",
             "msgid": "LEADER:", "msgctxt": None, "openlibrary_msgstr": "DIRETORE:"},
        ]
        restored, skipped = _restore_live_in_catalog(cat, findings)
        assert restored == 0 and skipped == 0
        assert cat["LEADER:"].string == "LEADER:"  # untouched

    def test_english_copy_or_empty_predicate(self):
        assert _english_copy_or_empty("", "X") is True
        assert _english_copy_or_empty("X", "X") is True          # english-copy
        assert _english_copy_or_empty("traducido", "X") is False  # genuine
        assert _english_copy_or_empty(("", ""), ("one", "many")) is True
        assert _english_copy_or_empty(("one", "many"), ("one", "many")) is True
        assert _english_copy_or_empty(("uno", "muchos"), ("one", "many")) is False


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
# _fix_plural_placeholder_defects / _repair_plural_form
# ---------------------------------------------------------------------------

class TestFixPluralPlaceholderDefects:
    """``./i18n fix`` self-heals the exact class that passes ``validate`` (it matches
    the singular msgid) but FAILS the bake-regression gate: a plural form reused for
    n>1 that hardcodes the number instead of carrying msgid_plural's placeholder.
    This is the damage that hit id/ja/ko/ru/uk in PR #188.

    Every case is proven against bake-regression's own detector — the same one the
    CI gate runs — so the test cannot pass while the gate would still reject: the
    defect is flagged BEFORE the fix and gone AFTER. The header carries ``Language:``
    because the detector compiles the catalog to a .mo and reads the plural rule back
    from it; without it the round-trip defaults to the Germanic (n!=1) rule and the
    single ja form would be mis-read as singular-only.
    """

    HEADER_JA = (
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        b'"Language: ja\\n"\n"Plural-Forms: nplurals=1; plural=0;\\n"\n\n'
    )
    MSGID = (
        'msgid "%(value)s, on 1 edition"\n'
        'msgid_plural "%(value)s, on %(count)d editions"\n'
    )
    KEY = ("%(value)s, on 1 edition", "%(value)s, on %(count)d editions")
    LIVE = {(None, "%(value)s, on 1 edition")}

    def _cat(self, msgstr0: str):
        return _catalog(self.HEADER_JA + (self.MSGID + f'msgstr[0] "{msgstr0}"\n').encode())

    def _gate(self, cat):
        """Findings from the exact detector the bake-regression check gates on."""
        return _mod._bake_regression().find_placeholder_defects("ja", cat, self.LIVE)

    def test_repairs_hardcoded_number_the_gate_rejects(self):
        cat = self._cat("%(value)s, sur 1 edition")  # dropped %(count)d, hardcoded 1
        assert len(self._gate(cat)) == 1  # prove the gate rejects it first
        repaired, cleared = _fix_plural_placeholder_defects(cat, "ja")
        assert repaired == ["%(value)s, on 1 edition"]
        assert cleared == []
        assert cat[self.KEY].string == ("%(value)s, sur %(count)d edition",)
        assert self._gate(cat) == []  # gate clean after

    def test_clears_when_two_bare_integers_make_the_swap_ambiguous(self):
        cat = self._cat("1 / 1 %(value)s")  # which integer is the count?
        assert len(self._gate(cat)) == 1
        repaired, cleared = _fix_plural_placeholder_defects(cat, "ja")
        assert repaired == []
        assert cleared == ["%(value)s, on 1 edition"]
        assert cat[self.KEY].string == ("",)  # cleared -> re-translated next loop
        assert self._gate(cat) == []

    def test_clears_cjk_number_the_regex_cannot_isolate(self):
        # "1件" — the digit abuts a CJK word char, so no unambiguous swap exists.
        cat = self._cat("%(value)s（1件の版より）")
        assert len(self._gate(cat)) == 1
        repaired, cleared = _fix_plural_placeholder_defects(cat, "ja")
        assert cleared == ["%(value)s, on 1 edition"]
        assert cat[self.KEY].string == ("",)
        assert self._gate(cat) == []

    def test_leaves_a_correct_form_untouched(self):
        cat = self._cat("%(value)s（%(count)d件の版より）")
        assert self._gate(cat) == []
        repaired, cleared = _fix_plural_placeholder_defects(cat, "ja")
        assert repaired == []
        assert cleared == []
        assert cat[self.KEY].string == ("%(value)s（%(count)d件の版より）",)

    def test_ignores_singular_only_messages(self):
        # Singular-only entries are _fix_format_errors' job; this must skip them.
        cat = _catalog(_po('msgid "Hello %(name)s"\nmsgstr "Hola"'))
        assert _fix_plural_placeholder_defects(cat, "xx") == ([], [])


class TestRepairPluralForm:
    def test_swaps_the_single_bare_integer(self):
        assert _repair_plural_form("on 1 edition", {"%(count)d"}) == "on %(count)d edition"

    def test_none_when_more_than_one_specifier_is_missing(self):
        assert _repair_plural_form("on 1 edition", {"%(count)d", "%(value)s"}) is None

    def test_none_when_not_exactly_one_bare_integer(self):
        assert _repair_plural_form("1 of 1", {"%(count)d"}) is None
        assert _repair_plural_form("no number here", {"%(count)d"}) is None

    def test_does_not_mistake_a_placeholder_digit_for_a_bare_integer(self):
        assert (
            _repair_plural_form("%(value)s and 1 more", {"%(count)d"})
            == "%(value)s and %(count)d more"
        )


# ---------------------------------------------------------------------------
# Scope guard: _did_translation_work / check_scope / isolate
# ---------------------------------------------------------------------------

class TestDidTranslationWork:
    def test_as_tuple_normalises_both_shapes(self):
        assert _as_tuple("x") == ("x",)
        assert _as_tuple(["a", "b"]) == ("a", "b")
        assert _as_tuple(("a",)) == ("a",)

    def test_new_non_empty_msgstr_is_work(self):
        base = _catalog(_po('msgid "A"\nmsgstr ""'))
        head = _catalog(_po('msgid "A"\nmsgstr "\xc3\x84"'))
        assert _did_translation_work(base, head) is True

    def test_changed_existing_msgstr_is_work(self):
        base = _catalog(_po('msgid "A"\nmsgstr "old"'))
        head = _catalog(_po('msgid "A"\nmsgstr "new"'))
        assert _did_translation_work(base, head) is True

    def test_identical_content_is_not_work(self):
        base = _catalog(_po('msgid "A"\nmsgstr "same"'))
        head = _catalog(_po('msgid "A"\nmsgstr "same"'))
        assert _did_translation_work(base, head) is False

    def test_added_empty_stub_is_not_work(self):
        # The #188 stray signature: a language gains entries but none translated.
        base = _catalog(_po('msgid "A"\nmsgstr "same"'))
        head = _catalog(_po('msgid "A"\nmsgstr "same"', 'msgid "B"\nmsgstr ""'))
        assert _did_translation_work(base, head) is False

    def test_clearing_a_translation_counts_as_change(self):
        # Removing content is still a content change -> the language stays in scope.
        base = _catalog(_po('msgid "A"\nmsgstr "was"'))
        head = _catalog(_po('msgid "A"\nmsgstr ""'))
        assert _did_translation_work(base, head) is True


def _init_locale_repo(tmp_path: Path, langs: dict[str, bytes]) -> tuple[Path, str]:
    """A throwaway git repo with locale/<lang>/messages.po committed as the base.
    Returns (root, base_sha). The scope guard runs its real git plumbing against it."""
    (tmp_path / "locale").mkdir()
    (tmp_path / "messages.pot").write_bytes(_po('msgid "A"\nmsgstr ""'))
    for lang, po in langs.items():
        d = tmp_path / "locale" / lang
        d.mkdir()
        (d / "messages.po").write_bytes(po)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, env=env, check=True,
                                    capture_output=True)
    run("init", "-q")
    run("add", "-A")
    run("commit", "-qm", "base")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                         capture_output=True, text=True).stdout.strip()
    return tmp_path, sha


class TestScopeGuardIntegration:
    """Runs the shipping check_scope/isolate against a real git repo (not a mock),
    so the git diff/show plumbing is exercised exactly as it is in CI."""

    def _setup(self, tmp_path, monkeypatch, langs):
        root, sha = _init_locale_repo(tmp_path, langs)
        monkeypatch.setattr(_mod, "REPO_ROOT", root)
        monkeypatch.setattr(_mod, "LOCALE_DIR", root / "locale")
        return root, sha

    def test_passes_when_every_changed_language_has_work(self, tmp_path, monkeypatch):
        root, sha = self._setup(tmp_path, monkeypatch, {
            "de": _po('msgid "A"\nmsgstr "alt"'),
            "fr": _po('msgid "A"\nmsgstr "ancien"'),
        })
        (root / "locale" / "de" / "messages.po").write_bytes(_po('msgid "A"\nmsgstr "neu"'))
        assert check_scope(ref=sha) == 0

    def test_fails_on_a_changed_language_with_no_work(self, tmp_path, monkeypatch, capsys):
        root, sha = self._setup(tmp_path, monkeypatch, {
            "de": _po('msgid "A"\nmsgstr "alt"'),
            "cs": _po('msgid "A"\nmsgstr "stary"'),
        })
        (root / "locale" / "de" / "messages.po").write_bytes(_po('msgid "A"\nmsgstr "neu"'))
        # cs: gains an empty stub (file changes) but translates nothing new.
        (root / "locale" / "cs" / "messages.po").write_bytes(
            _po('msgid "A"\nmsgstr "stary"', 'msgid "B"\nmsgstr ""'))
        assert check_scope(ref=sha) == 1
        assert "cs" in capsys.readouterr().out

    def test_allows_a_brand_new_language(self, tmp_path, monkeypatch):
        root, sha = self._setup(tmp_path, monkeypatch, {"de": _po('msgid "A"\nmsgstr "alt"')})
        d = root / "locale" / "sw"
        d.mkdir()
        (d / "messages.po").write_bytes(_po('msgid "A"\nmsgstr "mpya"'))
        assert check_scope(ref=sha) == 0

    def test_isolate_resets_other_languages_and_keeps_the_batch(self, tmp_path, monkeypatch):
        root, sha = self._setup(tmp_path, monkeypatch, {
            "de": _po('msgid "A"\nmsgstr "alt"'),
            "it": _po('msgid "A"\nmsgstr "vecchio"'),
        })
        (root / "locale" / "de" / "messages.po").write_bytes(_po('msgid "A"\nmsgstr "neu"'))
        (root / "locale" / "it" / "messages.po").write_bytes(_po('msgid "A"\nmsgstr "STRAY"'))
        isolate(["de"], base=sha)
        it = (root / "locale" / "it" / "messages.po").read_bytes()
        de = (root / "locale" / "de" / "messages.po").read_bytes()
        assert b"STRAY" not in it and b"vecchio" in it  # it reset to base
        assert b"neu" in de  # de (the batch) kept

    def test_fails_when_a_translation_pr_changes_the_pot(self, tmp_path, monkeypatch, capsys):
        root, sha = self._setup(tmp_path, monkeypatch, {"de": _po('msgid "A"\nmsgstr "alt"')})
        (root / "locale" / "de" / "messages.po").write_bytes(_po('msgid "A"\nmsgstr "neu"'))
        (root / "messages.pot").write_bytes(_po('msgid "A"\nmsgstr ""', 'msgid "B"\nmsgstr ""'))
        assert check_scope(ref=sha) == 1
        assert "messages.pot" in capsys.readouterr().out

    def test_allows_a_pot_only_change(self, tmp_path, monkeypatch):
        root, sha = self._setup(tmp_path, monkeypatch, {"de": _po('msgid "A"\nmsgstr "alt"')})
        (root / "messages.pot").write_bytes(_po('msgid "A"\nmsgstr ""', 'msgid "B"\nmsgstr ""'))
        assert check_scope(ref=sha) == 0

    def test_isolate_resets_the_pot(self, tmp_path, monkeypatch):
        root, sha = self._setup(tmp_path, monkeypatch, {"de": _po('msgid "A"\nmsgstr "alt"')})
        (root / "messages.pot").write_bytes(_po('msgid "A"\nmsgstr ""', 'msgid "NEW"\nmsgstr ""'))
        isolate(["de"], base=sha)
        assert b"NEW" not in (root / "messages.pot").read_bytes()

    def test_flags_a_stray_even_when_main_advanced_it_meanwhile(self, tmp_path, monkeypatch, capsys):
        """The exact #188 shape: while the batch PR is open, another PR merges to
        main and advances a language the batch only re-synced. Compared against
        main's TIP the batch's version differs (so it would escape); compared
        against the merge-base it correctly shows no work. The guard must flag it."""
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        root, base_sha = self._setup(tmp_path, monkeypatch, {
            "de": _po('msgid "A"\nmsgstr ""'),           # untranslated at branch point
            "it": _po('msgid "A"\nmsgstr "vecchio"'),    # already translated
        })

        def git(*a, **kw):
            return subprocess.run(["git", *a], cwd=root, env=env, check=True,
                                  capture_output=True, text=True, **kw)

        # main advances `it` with a brand-new real translation (a concurrent PR).
        git("checkout", "-qb", "mainline")
        (root / "locale" / "it" / "messages.po").write_bytes(
            _po('msgid "A"\nmsgstr "vecchio"', 'msgid "B"\nmsgstr "nuovo"'))
        git("commit", "-qam", "concurrent it translation")
        main_tip = git("rev-parse", "HEAD").stdout.strip()

        # The batch PR: branch from the original base, translate `de`, only re-sync
        # `it` (add the same empty stub for B, no translation) — the #188 stray.
        git("checkout", "-qb", "pr", base_sha)
        (root / "locale" / "de" / "messages.po").write_bytes(_po('msgid "A"\nmsgstr "neu"'))
        (root / "locale" / "it" / "messages.po").write_bytes(
            _po('msgid "A"\nmsgstr "vecchio"', 'msgid "B"\nmsgstr ""'))
        git("commit", "-qam", "batch: translate de, resync it")

        assert check_scope(ref=main_tip) == 1
        out = capsys.readouterr().out
        assert "it" in out  # it flagged as a stray despite main advancing it


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

    @staticmethod
    def _sync_in_tmp(tmp_path, lang, pot: bytes | None = None, po: bytes | None = None):
        """Run `./i18n sync lang` on a copy of the repo layout, never the real files.

        sync rewrites the .po in place; running it on the real locale/ reformatted
        a checked-in file on every test run (#44). Defaults to copies of the real
        messages.pot and .po; pass pot/po bytes to use a fixture instead.
        """
        import shutil
        shutil.copy2(_ROOT / "i18n", tmp_path / "i18n")
        (tmp_path / "locale" / lang).mkdir(parents=True)
        pot_path, po_path = tmp_path / "messages.pot", tmp_path / "locale" / lang / "messages.po"
        if pot is None:
            shutil.copy2(_ROOT / "messages.pot", pot_path)
        else:
            pot_path.write_bytes(pot)
        if po is None:
            shutil.copy2(LOCALE_DIR / lang / "messages.po", po_path)
        else:
            po_path.write_bytes(po)
        r = subprocess.run(
            [sys.executable, str(tmp_path / "i18n"), "sync", lang],
            capture_output=True, text=True, cwd=tmp_path,
        )
        return r, po_path

    def test_sync_suppresses_pybabel_stderr(self, tmp_path):
        """sync must suppress pybabel's 'updating catalog...' noise on stderr."""
        r, _ = self._sync_in_tmp(tmp_path, known_langs()[0])
        assert r.returncode == 0, r.stderr
        # pybabel writes "updating catalog..." to stderr; we suppress it with DEVNULL
        assert "updating catalog" not in r.stderr

    def test_sync_does_not_rewrap_long_lines(self, tmp_path):
        """sync keeps long msgids, msgstrs and comments on one line, like messages.pot (#44).

        pybabel update wraps at 76 by default, which turned every sync into a
        whole-file diff.
        """
        long_comment = "Translator note " + "that goes on " * 10 + "and on."
        long_id = "A source string " + "that is much longer " * 5 + "than 76 columns."
        long_str = "Eine Übersetzung " + "die viel länger ist " * 5 + "als 76 Spalten."
        pot = _po(f'#. {long_comment}\n#: some/template.html\nmsgid "{long_id}"\nmsgstr ""')
        po = _po(f'#: some/template.html\nmsgid "{long_id}"\nmsgstr "{long_str}"')
        r, po_path = self._sync_in_tmp(tmp_path, "de", pot=pot, po=po)
        assert r.returncode == 0, r.stderr
        text = po_path.read_text(encoding="utf-8")
        assert f"#. {long_comment}\n" in text
        assert f'msgid "{long_id}"\n' in text
        assert f'msgstr "{long_str}"\n' in text


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


# ---------------------------------------------------------------------------
# Partially-filled plurals with a reachable empty form (#102)
# ---------------------------------------------------------------------------

_RU_HEADER = (
    b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
    b'"Language: ru\\n"\n'
    b'"Plural-Forms: nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : '
    b'n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);\\n"\n\n'
)
_JA_HEADER = (
    b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
    b'"Language: ja\\n"\n"Plural-Forms: nplurals=1; plural=0;\\n"\n\n'
)
_PLURAL_PARTIAL = (
    b'msgid "%(n)d book"\nmsgid_plural "%(n)d books"\n'
    b'msgstr[0] "%(n)d kniga"\nmsgstr[1] "%(n)d knigi"\nmsgstr[2] ""\n'
)


class TestPartialPlurals:
    def test_reachable_empty_form_is_untranslated(self):
        cat = _catalog(_RU_HEADER + _PLURAL_PARTIAL)
        assert [e["id"] for e in _untranslated_from_catalog(cat)] == ["%(n)d book"]
        stats = _stats_from_catalog(cat, "ru")
        assert (stats["untranslated"], stats["translated"]) == (1, 0)

    def test_fully_filled_plural_is_translated(self):
        po = _PLURAL_PARTIAL.replace(b'msgstr[2] ""', b'msgstr[2] "%(n)d knig"')
        cat = _catalog(_RU_HEADER + po)
        assert _untranslated_from_catalog(cat) == []
        assert _stats_from_catalog(cat, "ru")["translated"] == 1

    def test_empty_unreachable_form_is_not_untranslated(self):
        # nplurals=1: only form 0 is ever selected, so an empty form 1 is harmless.
        po = (
            b'msgid "%(n)d book"\nmsgid_plural "%(n)d books"\n'
            b'msgstr[0] "%(n)d hon"\nmsgstr[1] ""\n'
        )
        cat = _catalog(_JA_HEADER + po)
        assert _untranslated_from_catalog(cat) == []
        assert _stats_from_catalog(cat, "ja")["untranslated"] == 0

    def test_apply_fills_all_forms(self):
        cat = _catalog(_RU_HEADER + _PLURAL_PARTIAL)
        forms = ["%(n)d kniga", "%(n)d knigi", "%(n)d knig"]
        assert _apply_to_catalog(cat, {"%(n)d book": forms}) == 1
        assert cat["%(n)d book"].string == tuple(forms)
        assert _untranslated_from_catalog(cat) == []

    def test_plural_entry_reports_nplurals(self):
        # The translating agent needs the form count: ru has 3, and a 2-form
        # answer would leave a reachable form empty, so the entry never leaves
        # `untranslated` and the agent's loop never ends.
        cat = _catalog(_RU_HEADER + _PLURAL_PARTIAL)
        [entry] = _untranslated_from_catalog(cat)
        assert entry["nplurals"] == 3
        assert _untranslated_from_catalog(_catalog(_po('msgid "Hi"\nmsgstr ""'))) == [{"id": "Hi"}]

    def test_two_forms_for_a_three_form_locale_stays_untranslated(self):
        cat = _catalog(_RU_HEADER + _PLURAL_PARTIAL)
        _apply_to_catalog(cat, {"%(n)d book": ["%(n)d kniga", "%(n)d knigi"]})
        assert [e["id"] for e in _untranslated_from_catalog(cat)] == ["%(n)d book"]


_AR_HEADER = (
    b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
    b'"Language: ar\\n"\n'
    b'"Plural-Forms: nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : '
    b'n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5);\\n"\n\n'
)
_PLURAL_EMPTY = (
    b'msgid "And 1 more."\nmsgid_plural "And %(count)d more."\n'
    b'msgstr[0] ""\nmsgstr[1] ""\n'
)


class TestApplyPluralForms:
    """#319: apply must not guess plural forms from a bare string or a list of
    the wrong length -- it rejects them so the agent notices."""

    def test_bare_string_rejected_for_six_form_locale(self):
        cat = _catalog(_AR_HEADER + _PLURAL_EMPTY)
        errors: list[str] = []
        assert _apply_to_catalog(cat, {"And 1 more.": "X"}, errors) == 0
        assert len(errors) == 1 and "exactly 6" in errors[0]
        assert [e["id"] for e in _untranslated_from_catalog(cat)] == ["And 1 more."]

    def test_six_forms_applied_for_ar(self):
        cat = _catalog(_AR_HEADER + _PLURAL_EMPTY)
        forms = [f"f{i} %(count)d" for i in range(6)]
        errors: list[str] = []
        assert _apply_to_catalog(cat, {"And 1 more.": forms}, errors) == 1
        assert errors == []
        assert cat["And 1 more."].string == tuple(forms)
        assert _untranslated_from_catalog(cat) == []

    def test_bare_string_rejected_for_three_form_locale(self):
        cat = _catalog(_RU_HEADER + _PLURAL_PARTIAL)
        errors: list[str] = []
        assert _apply_to_catalog(cat, {"%(n)d book": "%(n)d kniga"}, errors) == 0
        assert "a single string" in errors[0]
        assert cat["%(n)d book"].string == ("%(n)d kniga", "%(n)d knigi", "")

    def test_wrong_length_list_rejected(self):
        cat = _catalog(_RU_HEADER + _PLURAL_PARTIAL)
        errors: list[str] = []
        assert _apply_to_catalog(cat, {"%(n)d book": ["a", "b"]}, errors) == 0
        assert "got 2 form(s)" in errors[0]

    def test_bare_string_accepted_for_one_form_locale(self):
        cat = _catalog(_JA_HEADER + _PLURAL_EMPTY)
        errors: list[str] = []
        assert _apply_to_catalog(cat, {"And 1 more.": "ほか%(count)d件"}, errors) == 1
        assert errors == []
        assert cat["And 1 more."].string == ("ほか%(count)d件",)

    def test_plural_msgid_key_is_reported(self):
        # The run in #319 keyed the plural form by msgid_plural; it was ignored
        # silently. It should be reported, and so should the bare singular string.
        cat = _catalog(_AR_HEADER + _PLURAL_EMPTY)
        errors: list[str] = []
        _apply_to_catalog(cat, {"And 1 more.": "X", "And %(count)d more.": "Y"}, errors)
        assert len(errors) == 2
        assert any("is a msgid_plural" in e and "'And 1 more.'" in e for e in errors)

    def test_cli_exits_nonzero_on_rejection(self, tmp_path, monkeypatch):
        locale = tmp_path / "locale" / "ar"
        locale.mkdir(parents=True)
        (locale / "messages.po").write_bytes(_AR_HEADER + _PLURAL_EMPTY + b'\nmsgid "Hi"\nmsgstr ""\n')
        monkeypatch.setattr(_mod, "LOCALE_DIR", tmp_path / "locale")
        monkeypatch.setattr(_mod, "MESSAGES_POT", tmp_path / "missing.pot")
        trans = tmp_path / "t.json"
        trans.write_text(json.dumps({"Hi": "مرحبا", "And 1 more.": "X"}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["i18n", "apply", "ar", str(trans)])
        with pytest.raises(SystemExit) as exc:
            _mod.main()
        assert exc.value.code == 1
        # The valid entry is still written; only the bad plural is held back.
        cat = read_po(open(locale / "messages.po", "rb"))
        assert cat["Hi"].string == "مرحبا"
        assert not any(cat[("And 1 more.")].string)

class TestPotOverride:
    """#317/#322: translate.yml keeps the fresh messages.pot outside the checkout
    and points the toolbox at it with I18N_POT, so the working tree stays clean."""

    def test_untranslated_and_sync_read_i18n_pot(self, tmp_path):
        import shutil
        repo = tmp_path / "repo"
        (repo / "locale" / "xx").mkdir(parents=True)
        shutil.copy(_ROOT / "i18n", repo / "i18n")
        po = b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\nmsgid "Old"\nmsgstr "Alt"\n'
        (repo / "locale" / "xx" / "messages.po").write_bytes(po)
        (repo / "messages.pot").write_bytes(b'msgid ""\nmsgstr ""\n\nmsgid "Old"\nmsgstr ""\n')
        fresh = tmp_path / "fresh.pot"
        fresh.write_bytes(b'msgid ""\nmsgstr ""\n\nmsgid "Old"\nmsgstr ""\n\nmsgid "New"\nmsgstr ""\n')
        env = {**os.environ, "I18N_POT": str(fresh)}

        def run(*args):
            r = subprocess.run([sys.executable, str(repo / "i18n"), *args],
                               capture_output=True, text=True, cwd=repo, env=env)
            assert r.returncode == 0, r.stderr
            return r.stdout

        assert json.loads(run("untranslated", "xx")) == [{"id": "New"}]
        run("sync", "xx")
        cat = read_po(open(repo / "locale" / "xx" / "messages.po", "rb"))
        assert cat["New"] is not None and cat["Old"].string == "Alt"
        # The repo's own messages.pot is untouched.
        assert b"New" not in (repo / "messages.pot").read_bytes()



class TestFuzzyAsUntranslated:
    """#334: fuzzy entries are untranslated work, and apply clears the flag."""

    def test_fuzzy_entry_listed_without_old_text(self):
        cat = _catalog(_po('#, fuzzy\nmsgid "Staff"\nmsgstr "Statistiken"'))
        assert _untranslated_from_catalog(cat) == [{"id": "Staff"}]

    def test_fuzzy_plural_listed_with_nplurals(self):
        cat = _catalog(_RU_HEADER + b'#, fuzzy\n' + _PLURAL_PARTIAL.replace(b'msgstr[2] ""', b'msgstr[2] "x"'))
        [entry] = _untranslated_from_catalog(cat)
        assert entry["id"] == "%(n)d book" and entry["nplurals"] == 3

    def test_apply_clears_fuzzy_flag(self):
        cat = _catalog(_po('#, fuzzy\nmsgid "Staff"\nmsgstr "Statistiken"'))
        assert _apply_to_catalog(cat, {"Staff": "Mitarbeiter"}) == 1
        assert cat["Staff"].string == "Mitarbeiter"
        assert not cat["Staff"].fuzzy
        assert _untranslated_from_catalog(cat) == []

    def test_apply_keeps_other_flags(self):
        cat = _catalog(_po('#, fuzzy, python-format\nmsgid "%(n)s items"\nmsgstr "x"'))
        _apply_to_catalog(cat, {"%(n)s items": "%(n)s Einträge"})
        assert "python-format" in cat["%(n)s items"].flags
        assert not cat["%(n)s items"].fuzzy

    def test_counts_schedule_fuzzy_work(self):
        po = _po('msgid "A"\nmsgstr "a"', '#, fuzzy\nmsgid "B"\nmsgstr "c"')
        assert _batches_from_counts([_stats_from_catalog(_catalog(po), "de")]) == [["de"]]


class TestAgentIssueFixes:
    """#356: fix ignores never-translated plurals. #357: apply reports unmatched keys."""

    _PO = (
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
        'msgid "%(count)d work"\nmsgid_plural "%(count)d works"\nmsgstr[0] ""\nmsgstr[1] ""\n\n'
        'msgid "Hello"\nmsgstr ""\n'
    )

    def _catalog(self):
        from io import BytesIO
        from babel.messages.pofile import read_po
        return read_po(BytesIO(self._PO.encode()))

    def test_fix_skips_untranslated_plural(self):
        catalog = self._catalog()
        assert _fix_format_errors(catalog) == []

    def test_apply_reports_unmatched_key(self):
        catalog = self._catalog()
        errors: list[str] = []
        count = _apply_to_catalog(catalog, {"Hello": "Hallo", "Helo": "x"}, errors)
        assert count == 1
        assert len(errors) == 1 and "'Helo'" in errors[0] and "matches no msgid" in errors[0]

    def test_apply_plural_key_reported_once(self):
        catalog = self._catalog()
        errors: list[str] = []
        _apply_to_catalog(catalog, {"%(count)d works": ["a", "b"]}, errors)
        assert len(errors) == 1 and "msgid_plural" in errors[0]
