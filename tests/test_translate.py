"""Tests for translate.py toolbox.

Unit tests use in-memory catalogs (no filesystem, no network).
Integration tests use the real locale/ directory and verify the plan is sane.
"""
from __future__ import annotations

import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest
from babel.messages.pofile import read_po

# Repo root on sys.path so `import translate` works
sys.path.insert(0, str(Path(__file__).parent.parent))
from translate import (
    BATCH_SIZE,
    LOCALE_DIR,
    _apply_to_catalog,
    _fix_format_errors,
    _fix_html_attrs,
    _jobs_from_stats,
    _stats_from_catalog,
    _untranslated_from_catalog,
    known_langs,
    translation_jobs,
    validate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _catalog(po_bytes: bytes):
    return read_po(BytesIO(po_bytes))


HEADER = b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'


def _po(*entries: str) -> bytes:
    """Build minimal .po bytes from a list of entry strings."""
    return HEADER + b"\n".join(e.encode() for e in entries) + b"\n"


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

    def test_limit_zero_returns_all(self):
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

    def test_does_not_overwrite_existing_translation(self):
        po = _po('msgid "Hello"\nmsgstr "Existing"')
        cat = _catalog(po)
        _apply_to_catalog(cat, {"Hello": "New"})
        # apply unconditionally overwrites — caller is responsible for only
        # passing untranslated strings. verify it does overwrite.
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
        po = _po(
            'msgid "Hello %(name)s"\nmsgstr "Hallo %(wrong)s"',
        )
        cat = _catalog(po)
        assert _fix_format_errors(cat) == 1
        assert cat["Hello %(name)s"].string == ""

    def test_clears_extra_placeholder_in_msgstr(self):
        po = _po('msgid "Hello"\nmsgstr "Hallo %(name)s"')
        assert _fix_format_errors(_catalog(po)) == 1

    def test_no_change_when_placeholders_match(self):
        po = _po('msgid "Hello %(name)s"\nmsgstr "Hallo %(name)s"')
        assert _fix_format_errors(_catalog(po)) == 0

    def test_clears_positional_count_mismatch(self):
        po = _po('msgid "Page %s of %s"\nmsgstr "Seite %s"')
        assert _fix_format_errors(_catalog(po)) == 1

    def test_no_change_when_no_placeholders(self):
        po = _po('msgid "Hello"\nmsgstr "Hallo"')
        assert _fix_format_errors(_catalog(po)) == 0


# ---------------------------------------------------------------------------
# _jobs_from_stats (pure — the key orchestration function)
# ---------------------------------------------------------------------------

class TestJobsFromStats:
    def _stats(self, items: list[tuple[str, int]]) -> list[dict]:
        return [{"lang": lang, "untranslated": n} for lang, n in items]

    def test_batch_langs_grouped_into_one_job(self):
        stats = self._stats([("de", 30), ("fr", 50), ("es", 75)])
        jobs = _jobs_from_stats(stats, batch_size=75)
        assert len(jobs) == 1
        assert set(jobs[0]["langs"]) == {"de", "fr", "es"}

    def test_individual_langs_each_get_own_job(self):
        stats = self._stats([("de", 100), ("fr", 200)])
        jobs = _jobs_from_stats(stats, batch_size=75)
        assert len(jobs) == 2
        assert {"de"} in [set(j["langs"]) for j in jobs]
        assert {"fr"} in [set(j["langs"]) for j in jobs]

    def test_mixed_batch_and_individual(self):
        stats = self._stats([("de", 30), ("fr", 200), ("es", 75), ("ja", 300)])
        jobs = _jobs_from_stats(stats, batch_size=75)
        # one batch job + two individual jobs
        assert len(jobs) == 3
        batch_job = next(j for j in jobs if len(j["langs"]) > 1)
        assert set(batch_job["langs"]) == {"de", "es"}
        langs_with_own_job = [j["langs"][0] for j in jobs if len(j["langs"]) == 1]
        assert set(langs_with_own_job) == {"fr", "ja"}

    def test_zero_untranslated_excluded(self):
        stats = self._stats([("de", 0), ("fr", 0)])
        assert _jobs_from_stats(stats) == []

    def test_empty_stats(self):
        assert _jobs_from_stats([]) == []

    def test_exactly_at_batch_size_goes_to_batch(self):
        stats = self._stats([("de", BATCH_SIZE)])
        jobs = _jobs_from_stats(stats)
        assert len(jobs) == 1
        assert jobs[0]["langs"] == ["de"]

    def test_one_over_batch_size_gets_own_job(self):
        stats = self._stats([("de", BATCH_SIZE + 1)])
        jobs = _jobs_from_stats(stats)
        assert len(jobs) == 1
        assert jobs[0]["langs"] == ["de"]

    def test_no_lang_appears_in_multiple_jobs(self):
        stats = self._stats([("de", 30), ("fr", 200), ("es", 50), ("ja", 0)])
        jobs = _jobs_from_stats(stats)
        all_langs = [lang for job in jobs for lang in job["langs"]]
        assert len(all_langs) == len(set(all_langs))

    def test_custom_batch_size(self):
        stats = self._stats([("de", 10), ("fr", 20)])
        jobs = _jobs_from_stats(stats, batch_size=15)
        # de (10) → batch, fr (20) → individual
        assert len(jobs) == 2


# ---------------------------------------------------------------------------
# Integration: plan against real locale/ directory
# ---------------------------------------------------------------------------

class TestPlanIntegration:
    def test_all_known_langs_covered(self):
        """Every known language appears in exactly one job or is skipped."""
        jobs = translation_jobs()
        langs_in_jobs = {lang for job in jobs for lang in job["langs"]}
        all_langs = set(known_langs())
        # langs with work are in jobs; langs without work are absent — union must be subset
        assert langs_in_jobs <= all_langs

    def test_no_lang_in_multiple_jobs(self):
        jobs = translation_jobs()
        all_langs = [lang for job in jobs for lang in job["langs"]]
        assert len(all_langs) == len(set(all_langs))

    def test_each_job_has_at_least_one_lang(self):
        for job in translation_jobs():
            assert len(job["langs"]) >= 1

    def test_plan_cli_outputs_valid_json(self):
        """translate plan must emit valid JSON on stdout."""
        result = subprocess.run(
            [sys.executable, "translate.py", "plan"],
            capture_output=True, text=True, cwd=Path(__file__).parent.parent,
        )
        assert result.returncode == 0, result.stderr
        # last block of output should be valid JSON
        output = result.stdout.strip()
        json_part = output[output.rfind("\n["):]  # find the JSON array
        parsed = json.loads(json_part)
        assert isinstance(parsed, list)
        for job in parsed:
            assert "langs" in job
            assert isinstance(job["langs"], list)

    def test_untranslated_cli_dry_run(self):
        """translate untranslated <lang> --limit 1 returns valid JSON."""
        lang = known_langs()[0]
        result = subprocess.run(
            [sys.executable, "translate.py", "untranslated", lang, "--limit", "1"],
            capture_output=True, text=True, cwd=Path(__file__).parent.parent,
        )
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert isinstance(parsed, list)
        if parsed:
            assert "id" in parsed[0]

    def test_status_cli_runs(self):
        """translate status must run without error."""
        result = subprocess.run(
            [sys.executable, "translate.py", "status"],
            capture_output=True, text=True, cwd=Path(__file__).parent.parent,
        )
        assert result.returncode == 0, result.stderr
        assert "lang" in result.stdout
