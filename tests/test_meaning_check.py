"""Tests for scripts/meaning_check.py (#331): what gets sent to the agent, and how
its untrusted findings become a PR comment."""
from __future__ import annotations

import importlib.util
import json
import sys
from io import BytesIO
from pathlib import Path

from babel.messages.pofile import read_po

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("meaning_check", _ROOT / "scripts" / "meaning_check.py")
mc = importlib.util.module_from_spec(_spec)
sys.modules["meaning_check"] = mc
_spec.loader.exec_module(mc)

HEADER = (
    b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
    b'"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
)


def _cat(*entries: str):
    return read_po(BytesIO(HEADER + "\n".join(entries).encode() + b"\n"))


BASE = _cat(
    'msgid "Hello"\nmsgstr "Hallo"',
    'msgid "Staff"\nmsgstr ""',
    'msgid "Trending"\nmsgstr "Beliebt"',
)


class TestChangedEntries:
    def test_only_new_or_changed_translations(self):
        head = _cat(
            'msgid "Hello"\nmsgstr "Hallo"',            # unchanged
            'msgid "Staff"\nmsgstr "Statistiken"',      # newly translated
            'msgid "Trending"\nmsgstr "Im Trend"',      # changed
        )
        got = mc.changed_entries(BASE, head, "de")
        assert [(e["msgid"], e["translation"]) for e in got] == [
            ("Staff", "Statistiken"), ("Trending", "Im Trend")]
        assert all(e["lang"] == "de" and e["key"].startswith("de:") for e in got)

    def test_skips_fuzzy_and_cleared(self):
        head = _cat(
            'msgid "Hello"\nmsgstr ""',                          # cleared
            '#, fuzzy\nmsgid "Staff"\nmsgstr "Statistiken"',     # fuzzy: not shown
            'msgid "Trending"\nmsgstr "Beliebt"',
        )
        assert mc.changed_entries(BASE, head, "de") == []

    def test_new_language_checks_every_translation(self):
        head = _cat('msgid "Hello"\nmsgstr "Hallo"', 'msgid "Staff"\nmsgstr ""')
        assert [e["msgid"] for e in mc.changed_entries(None, head, "xx")] == ["Hello"]

    def test_plural_and_context(self):
        head = _cat(
            '#. Shelf label on the reading log.\n#: account/books.html:3\n'
            'msgid "%(n)d book"\nmsgid_plural "%(n)d books"\n'
            'msgstr[0] "%(n)d Buch"\nmsgstr[1] "%(n)d Bücher"',
        )
        [e] = mc.changed_entries(None, head, "de")
        assert e["msgid_plural"] == "%(n)d books"
        assert e["translation"] == ["%(n)d Buch", "%(n)d Bücher"]
        assert e["context"] == "Shelf label on the reading log."
        assert e["used_in"] == ["account/books.html"]


class TestAllAndSample:
    def test_all_entries_is_every_shown_translation(self):
        assert [e["msgid"] for e in mc.all_entries(BASE, "de")] == ["Hello", "Trending"]

    def test_sample_is_even_and_deterministic(self):
        items = [{"key": str(i)} for i in range(10)]
        assert [e["key"] for e in mc.sample(items, 5)] == ["0", "2", "4", "6", "8"]
        assert mc.sample(items, 0) == items
        assert mc.sample(items, 50) == items


class TestWriteEntries:
    def test_one_entry_per_line_and_valid_json(self, tmp_path):
        doc = {"total": 3, "checked": 2, "entries": [{"key": "de:1", "msgid": "a|b"}, {"key": "de:2", "msgid": "ü"}]}
        out = tmp_path / "e.json"
        mc.write_entries(doc, out)
        assert json.loads(out.read_text(encoding="utf-8")) == doc
        assert len(out.read_text(encoding="utf-8").splitlines()) == 4  # header, 2 entries, footer


ENTRIES = [
    {"key": "de:1", "lang": "de", "msgid": "Staff", "translation": "Statistiken"},
    {"key": "de:2", "lang": "de", "msgid": "Hello", "translation": "Hallo"},
]
DOC = {"total": 2, "checked": 2, "entries": ENTRIES}


def _findings(tmp_path, data) -> Path:
    p = tmp_path / "f.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return p


class TestLoadFindings:
    def test_keeps_only_checked_keys_once(self, tmp_path):
        p = _findings(tmp_path, {"findings": [
            {"key": "de:1", "back_translation": "Statistics", "problem": "wrong"},
            {"key": "de:1", "problem": "duplicate"},
            {"key": "xx:9", "problem": "made up"},
            "not a dict",
        ]})
        got = mc.load_findings(p, ENTRIES)
        assert [f["entry"]["key"] for f in got] == ["de:1"]
        assert got[0]["back_translation"] == "Statistics"

    def test_missing_or_bad_file_is_none(self, tmp_path):
        assert mc.load_findings(tmp_path / "nope.json", ENTRIES) is None
        assert mc.load_findings(_findings(tmp_path, "{not json"), ENTRIES) is None
        assert mc.load_findings(_findings(tmp_path, {"summary": "x"}), ENTRIES) is None

    def test_empty_findings(self, tmp_path):
        assert mc.load_findings(_findings(tmp_path, {"findings": []}), ENTRIES) == []


class TestRender:
    def test_findings_table(self):
        f = [{"entry": ENTRIES[0], "back_translation": "Statistics", "problem": "Means statistics."}]
        out = mc.render(DOC, f)
        assert "**1 may not mean what the English says.**" in out
        assert "| de | Staff | Statistiken | Statistics | Means statistics. |" in out
        assert "does not block" in out

    def test_clean_and_unfinished(self):
        assert "None look wrong" in mc.render(DOC, [])
        assert "did not finish" in mc.render(DOC, None)

    def test_sampled_scope(self):
        assert "Checked 2 of 9" in mc.render({"total": 9, "checked": 2, "entries": ENTRIES}, [])

    def test_untrusted_text_is_escaped(self):
        nasty = {"key": "de:1", "lang": "de", "msgid": "a|b", "translation": "<script>x</script> @someone `cmd`\nnewline"}
        out = mc.render(DOC, [{"entry": nasty, "back_translation": "x" * 1000, "problem": "p"}])
        row = [l for l in out.splitlines() if l.startswith("| de |")][0]
        assert "a\\|b" in row
        assert "<script>" not in row and "&lt;script&gt;" in row
        assert "@someone" not in row  # no GitHub mention
        assert "`" not in row and "\n" not in row
        assert "x" * 200 not in row  # length-limited


class TestReportCLI:
    def test_report_prints_comment(self, tmp_path, capsys):
        entries = tmp_path / "e.json"
        mc.write_entries(DOC, entries)
        findings = _findings(tmp_path, {"findings": [{"key": "de:1", "problem": "wrong"}]})
        assert mc.main(["report", "--entries", str(entries), "--findings", str(findings)]) == 0
        assert "| de | Staff | Statistiken |" in capsys.readouterr().out
