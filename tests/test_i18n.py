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


# ---------------------------------------------------------------------------
# _batches_from_counts (pure — the key orchestration function)
# ---------------------------------------------------------------------------

class TestBatchesFromCounts:
    def _counts(self, items: list[tuple[str, int]]) -> list[dict]:
        return [{"lang": lang, "untranslated": n} for lang, n in items]

    def test_every_language_gets_its_own_batch(self):
        counts = self._counts([("de", 30), ("fr", 50), ("es", 75)])
        batches = _batches_from_counts(counts)
        assert len(batches) == 3
        assert sorted(batches) == [["de"], ["es"], ["fr"]]

    def test_large_and_small_langs_all_get_own_batch(self):
        counts = self._counts([("de", 30), ("fr", 200), ("es", 75), ("ja", 300)])
        batches = _batches_from_counts(counts)
        assert sorted(batches) == [["de"], ["es"], ["fr"], ["ja"]]

    def test_zero_untranslated_excluded(self):
        counts = self._counts([("de", 0), ("fr", 0)])
        assert _batches_from_counts(counts) == []

    def test_empty_counts(self):
        assert _batches_from_counts([]) == []

    def test_no_lang_in_multiple_batches(self):
        counts = self._counts([("de", 30), ("fr", 200), ("es", 50), ("ja", 0)])
        batches = _batches_from_counts(counts)
        all_langs = [lang for batch in batches for lang in batch]
        assert len(all_langs) == len(set(all_langs))


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
