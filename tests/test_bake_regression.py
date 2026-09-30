"""Tests for scripts/bake_regression.py.

Synthetic catalogs only: no network, no dependence on the real locale/ files.
The known-answer controls each build a pair with one regression, assert the
check fails, remove the regression, and assert it passes.
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

_ROOT = Path(__file__).parent.parent
# Overridable so the CLI-level tests can be pointed at an older revision of the
# script to watch them fail first.
_SCRIPT = Path(os.environ.get("BAKE_REGRESSION_SCRIPT", _ROOT / "scripts" / "bake_regression.py"))
_spec = importlib.util.spec_from_file_location("bake_regression", _SCRIPT)
br = importlib.util.module_from_spec(_spec)
sys.modules["bake_regression"] = br  # dataclasses resolve types via sys.modules
_spec.loader.exec_module(br)

TWO_FORMS = "nplurals=2; plural=(n != 1);"
PL_FORMS = ("nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && "
            "(n%100<10 || n%100>=20) ? 1 : 2);")
RU_FORMS = ("nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
            "(n%100<10 || n%100>=20) ? 1 : 2);")
ONE_FORM = "nplurals=1; plural=0;"


def _po_text(*entries: str, plural_forms: str | None = TWO_FORMS,
             language: str | None = "de") -> str:
    header = 'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
    # babel only writes Plural-Forms into the .mo when the catalog has a Language.
    if plural_forms and language:
        header += f'"Language: {language}\\n"\n'
    if plural_forms:
        header += f'"Plural-Forms: {plural_forms}\\n"\n'
    return header + "\n" + "\n\n".join(entries) + "\n"


def _cat(*entries: str, plural_forms: str | None = TWO_FORMS, language: str | None = "de"):
    text = _po_text(*entries, plural_forms=plural_forms, language=language)
    return read_po(BytesIO(text.encode()))


def _live(*entries: str):
    return br.live_keys_from_pot(_cat(*entries))


ENABLE = 'msgid "Enable"\nmsgstr "Activar"'
ENABLE_FUZZY = '#, fuzzy\nmsgid "Enable"\nmsgstr "Ejemplo"'
WAITING_ID = ('msgid "There is %(count)d person waiting for this book."\n'
              'msgid_plural "There are %(count)d people waiting for this book."')
WAITING_OL = (WAITING_ID + '\nmsgstr[0] "Czeka %(count)d osoba na tę książkę."'
              '\nmsgstr[1] "Czekają %(count)d osoby na tę książkę."')
WAITING_PL_EMPTY_3RD = WAITING_OL + '\nmsgstr[2] ""'
WAITING_PL_FULL = WAITING_OL + '\nmsgstr[2] "Czeka %(count)d osób na tę książkę."'


def _regressions(ol, baked, live, lang="xx"):
    return br.find_regressions(lang, ol, baked, live)


# ---------------------------------------------------------------------------
# Known-answer controls: red with the regression, green without it
# ---------------------------------------------------------------------------

class TestKnownAnswerControls:
    def test_fuzzy_on_baked_side_is_a_regression_then_fixed(self):
        live = _live('msgid "Enable"\nmsgstr ""')
        ol = _cat(ENABLE)
        red = _regressions(ol, _cat(ENABLE_FUZZY), live, "es")
        assert [(f.msgid, f.reason) for f in red] == [("Enable", "fuzzy")]
        assert _regressions(ol, _cat(ENABLE), live, "es") == []

    def test_empty_third_plural_form_is_a_regression_then_fixed(self):
        # openlibrary's pl had no Plural-Forms header (babel defaults to 2 forms);
        # this repo declares 3 and left the third empty.
        live = _live(WAITING_ID + '\nmsgstr[0] ""\nmsgstr[1] ""')
        ol = _cat(WAITING_OL, plural_forms=None)
        red = _regressions(ol, _cat(WAITING_PL_EMPTY_3RD, plural_forms=PL_FORMS), live, "pl")
        assert len(red) == 1
        assert red[0].reason == "empty-plural-form"
        assert 5 in red[0].n or 0 in red[0].n
        assert 1 not in red[0].n and 2 not in red[0].n
        green = _regressions(ol, _cat(WAITING_PL_FULL, plural_forms=PL_FORMS), live, "pl")
        assert green == []

    def test_cli_exits_1_on_regression_and_0_once_removed(self, tmp_path):
        ol_dir, locale = _write_pair(tmp_path, ol={"es": [ENABLE]}, baked={"es": [ENABLE_FUZZY]})
        red = _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", "-")
        assert red.returncode == 1, red.stderr
        report = json.loads(red.stdout)
        assert report["totals"]["regressions"] == 1
        assert report["languages"]["es"]["openlibrary_translated"] == 1

        (locale / "es" / "messages.po").write_text(_po_text(ENABLE))
        green = _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", "-")
        assert green.returncode == 0, green.stderr
        assert json.loads(green.stdout)["totals"]["regressions"] == 0


# ---------------------------------------------------------------------------
# What openlibrary actually renders
# ---------------------------------------------------------------------------

class TestRendering:
    def test_write_mo_compiles_an_empty_plural_form_as_english(self):
        # The trap: the .mo value is non-empty, and it is the English msgid_plural.
        t = br.compile_catalog(_cat(WAITING_PL_EMPTY_3RD, plural_forms=PL_FORMS))
        s1 = "There is %(count)d person waiting for this book."
        s2 = "There are %(count)d people waiting for this book."
        assert t.ngettext(s1, s2, 5) == s2
        assert not br.renders_translation(t, None, s1, s2, 5)
        assert br.renders_translation(t, None, s1, s2, 2)

    def test_fuzzy_is_dropped_at_compile(self):
        t = br.compile_catalog(_cat(ENABLE_FUZZY))
        assert not br.renders_translation(t, None, "Enable", None, None)
        assert br.renders_translation(br.compile_catalog(_cat(ENABLE)), None, "Enable", None, None)

    def test_has_translation_agrees_with_the_compiled_catalog(self):
        # For translations that differ from the English, the catalog-state rule
        # and a real .mo lookup must give the same answer at every probed n.
        cases = [
            (_cat(WAITING_PL_EMPTY_3RD, plural_forms=PL_FORMS), WAITING_PL_EMPTY_3RD),
            (_cat(WAITING_PL_FULL, plural_forms=PL_FORMS), WAITING_PL_FULL),
            (_cat(WAITING_OL, plural_forms=None), WAITING_OL),
            (_cat(WAITING_OL, plural_forms=PL_FORMS), WAITING_OL),
        ]
        s1 = "There is %(count)d person waiting for this book."
        s2 = "There are %(count)d people waiting for this book."
        checked = 0
        for catalog, _ in cases:
            t = br.compile_catalog(catalog)
            message = catalog.get(s1)
            for n in range(30):
                assert br.has_translation(t, message, None, s1, s2, n) == br.renders_translation(
                    t, None, s1, s2, n), (catalog.plural_forms, n)
                checked += 1
        assert checked == 120

    def test_plural_forms_without_language_compile_as_two_forms(self):
        # So the plural rule must be read from the compiled catalog, not the .po.
        catalog = _cat(WAITING_PL_FULL, plural_forms=PL_FORMS, language=None)
        assert catalog.num_plurals == 3
        t = br.compile_catalog(catalog)
        assert {t.plural(n) for n in range(100)} == {0, 1}

    def test_plural_probes_cover_every_form_of_both_rules(self):
        ol = br.compile_catalog(_cat(plural_forms=None))
        baked = br.compile_catalog(_cat(plural_forms=PL_FORMS))
        probes = br.plural_probes(ol, baked)
        assert {baked.plural(n) for n in probes} == {0, 1, 2}
        assert {ol.plural(n) for n in probes} == {0, 1}


# ---------------------------------------------------------------------------
# Regression rules
# ---------------------------------------------------------------------------

class TestRegressions:
    def test_missing_entry(self):
        live = _live('msgid "Enable"\nmsgstr ""')
        [f] = _regressions(_cat(ENABLE), _cat(), live)
        assert f.reason == "missing"

    def test_empty_msgstr(self):
        live = _live('msgid "Enable"\nmsgstr ""')
        [f] = _regressions(_cat(ENABLE), _cat('msgid "Enable"\nmsgstr ""'), live)
        assert f.reason == "empty"

    def test_fuzzy_in_openlibrary_is_not_translated_there(self):
        live = _live('msgid "Enable"\nmsgstr ""')
        assert _regressions(_cat(ENABLE_FUZZY), _cat(), live) == []

    def test_only_live_msgids_count(self):
        live = _live('msgid "Something else"\nmsgstr ""')
        assert _regressions(_cat(ENABLE), _cat(), live) == []

    def test_keyed_on_msgctxt(self):
        entry = 'msgctxt "button"\nmsgid "Enable"\nmsgstr "Activar"'
        live = _live('msgctxt "button"\nmsgid "Enable"\nmsgstr ""')
        # Same msgid without the context does not satisfy it.
        [f] = _regressions(_cat(entry), _cat(ENABLE), live)
        assert f.msgctxt == "button"
        assert _regressions(_cat(entry), _cat(entry), live) == []

    def test_plural_with_fewer_msgstrs_than_forms(self):
        live = _live(WAITING_ID + '\nmsgstr[0] ""\nmsgstr[1] ""')
        # Two msgstrs under a three-form rule: read_po pads the third with "".
        [f] = _regressions(_cat(WAITING_OL, plural_forms=None),
                           _cat(WAITING_OL, plural_forms=PL_FORMS, language="pl"), live)
        assert f.reason == "empty-plural-form"
        assert 5 in f.n or 0 in f.n

    def test_translation_identical_to_english_counts_but_is_not_visible(self):
        live = _live('msgid "PR"\nmsgstr ""')
        [f] = _regressions(_cat('msgid "PR"\nmsgstr "PR"'),
                           _cat('#, fuzzy\nmsgid "PR"\nmsgstr "Anterior"'), live)
        assert f.visible is False

    def test_visible_regression_is_flagged_visible(self):
        live = _live('msgid "Enable"\nmsgstr ""')
        [f] = _regressions(_cat(ENABLE), _cat(ENABLE_FUZZY), live)
        assert f.visible is True


# ---------------------------------------------------------------------------
# Placeholder rules
# ---------------------------------------------------------------------------

DAYS_ID = 'msgid "Waiting for 1 day"\nmsgid_plural "Waiting for %(count)d days"'


def _placeholders(baked, live=None):
    live = live if live is not None else br.live_keys_from_pot(baked)
    return br.find_placeholder_defects("xx", baked, live)


class TestPlaceholders:
    def test_singular_mismatch(self):
        [f] = _placeholders(_cat('msgid "by %(name)s"\nmsgstr "par %(nom)s"'))
        assert f.kind == "placeholder"

    def test_positional_order_and_count(self):
        assert _placeholders(_cat('msgid "%s of %d"\nmsgstr "%d von %s"'))
        assert _placeholders(_cat('msgid "%s and %s"\nmsgstr "%s"'))
        assert _placeholders(_cat('msgid "%s of %d"\nmsgstr "%s von %d"')) == []

    def test_named_placeholders_may_repeat_and_reorder(self):
        entry = 'msgid "%(a)s and %(b)s"\nmsgstr "%(b)s, %(a)s, %(a)s"'
        assert _placeholders(_cat(entry)) == []

    def test_percent_literal_is_not_a_placeholder(self):
        assert _placeholders(_cat('msgid "100%% done"\nmsgstr "100 %% fait"')) == []

    def test_fuzzy_and_empty_are_skipped(self):
        assert _placeholders(_cat('#, fuzzy\nmsgid "by %(name)s"\nmsgstr "par"')) == []
        assert _placeholders(_cat('msgid "by %(name)s"\nmsgstr ""')) == []

    def test_form_selected_only_by_one_may_match_the_singular(self):
        entry = DAYS_ID + '\nmsgstr[0] "Warten seit 1 Tag"\nmsgstr[1] "Warten seit %(count)d Tagen"'
        assert _placeholders(_cat(entry)) == []

    def test_other_forms_must_match_msgid_plural(self):
        entry = DAYS_ID + '\nmsgstr[0] "Čekám 1 den"\nmsgstr[1] "Čekám 1 den"\nmsgstr[2] ""'
        [f] = _placeholders(_cat(entry, plural_forms=PL_FORMS))
        assert "msgstr[1]" in f.reason

    def test_single_form_language_must_carry_the_count(self):
        # openlibrary-i18n#15: with nplurals=1 the only form is shown for every n.
        entry = DAYS_ID + '\nmsgstr[0] "1日待っています"'
        [f] = _placeholders(_cat(entry, plural_forms=ONE_FORM))
        assert 5 in f.n or 2 in f.n
        ok = DAYS_ID + '\nmsgstr[0] "%(count)d日待っています"'
        assert _placeholders(_cat(ok, plural_forms=ONE_FORM)) == []

    def test_form_zero_shared_with_twenty_one(self):
        entry = (DAYS_ID + '\nmsgstr[0] "Ожидает 1 день"\nmsgstr[1] "Ожидает %(count)d дня"'
                 '\nmsgstr[2] "Ожидает %(count)d дней"')
        [f] = _placeholders(_cat(entry, plural_forms=RU_FORMS))
        assert 21 in f.n
        fixed = entry.replace("Ожидает 1 день", "Ожидает %(count)d день")
        assert _placeholders(_cat(fixed, plural_forms=RU_FORMS)) == []

    def test_only_live_msgids(self):
        entry = 'msgid "by %(name)s"\nmsgstr "par %(nom)s"'
        assert _placeholders(_cat(entry), live=set()) == []

    def test_form_selected_only_by_zero_may_drop_the_count(self):
        ar_forms = ("nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : "
                    "n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5);")
        forms = ['"لا أيام"', '"يوم واحد"', '"يومان"', '"%(count)d أيام"',
                 '"%(count)d يومًا"', '"%(count)d يوم"']
        entry = DAYS_ID + "".join(f"\nmsgstr[{i}] {f}" for i, f in enumerate(forms))
        # n=2 selects msgstr[2] alone, and "two days" without the count is a defect
        # under the rule; only the n=0 form is exempt.
        [f] = _placeholders(_cat(entry, plural_forms=ar_forms, language="ar"))
        assert "msgstr[2]" in f.reason and "msgstr[0]" not in f.reason


class TestPluralRule:
    def test_rule_that_never_selects_a_declared_form(self):
        broken = "nplurals=3; plural=(n > 2);"
        [f] = br.find_plural_rule_defects("ar", _cat(plural_forms=broken, language="ar"))
        assert "msgstr[2]" in f.reason and f.gating is False
        assert br.find_plural_rule_defects("pl", _cat(plural_forms=PL_FORMS, language="pl")) == []


class TestPlaceholderGating:
    """A placeholder defect gates only when openlibrary does not already ship it."""

    # Drops the placeholder: wrong on the page, but formatting does not raise,
    # so the bake still installs the locale.
    ENTRY_BAD = 'msgid "by %(name)s"\nmsgstr "par"'
    ENTRY_GOOD = 'msgid "by %(name)s"\nmsgstr "par %(name)s"'

    def _run(self, tmp_path, ol_entry):
        ol_dir, locale = _write_pair(
            tmp_path, ol={"fr": [ENABLE] + ([ol_entry] if ol_entry else [])},
            baked={"fr": [ENABLE, self.ENTRY_BAD]},
            pot=[ENABLE, self.ENTRY_GOOD])
        out = _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", "-")
        return out.returncode, json.loads(out.stdout)["totals"]

    def test_new_defect_gates(self, tmp_path):
        code, totals = self._run(tmp_path, self.ENTRY_GOOD)
        assert code == 1
        assert totals["placeholder_defects_new"] == 1 and totals["placeholder_defects"] == 1

    def test_defect_openlibrary_does_not_translate_gates(self, tmp_path):
        code, totals = self._run(tmp_path, None)
        assert code == 1 and totals["placeholder_defects_new"] == 1

    def test_defect_openlibrary_already_ships_is_reported_not_gating(self, tmp_path):
        code, totals = self._run(tmp_path, self.ENTRY_BAD)
        assert code == 0
        assert totals["placeholder_defects_new"] == 0 and totals["placeholder_defects"] == 1


# ---------------------------------------------------------------------------
# CLI and report
# ---------------------------------------------------------------------------

def _write_pair(tmp_path, ol: dict, baked: dict, pot: list[str] | None = None):
    ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
    entries = pot if pot is not None else sorted(
        {e for es in list(ol.values()) + list(baked.values()) for e in es})
    blank = []
    for e in entries:
        head = e.split("\nmsgstr")[0].replace("#, fuzzy\n", "")
        blank.append(head + ('\nmsgstr[0] ""\nmsgstr[1] ""' if "msgid_plural" in head
                             else '\nmsgstr ""'))
    ol_dir.mkdir()
    (ol_dir / "messages.pot").write_text(_po_text(*blank))
    for side, root in ((ol, ol_dir), (baked, locale)):
        for lang, es in side.items():
            (root / lang).mkdir(parents=True)
            (root / lang / "messages.po").write_text(_po_text(*es))
    return ol_dir, locale


def _cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_SCRIPT), *map(str, args)],
                          capture_output=True, text=True, cwd=_ROOT)


class TestCLI:
    def test_report_names_both_sides(self, tmp_path):
        ol_dir, locale = _write_pair(tmp_path, ol={"es": [ENABLE]}, baked={"es": [ENABLE_FUZZY]})
        out = _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--list")
        assert out.returncode == 1
        assert "regressions to English: 1" in out.stdout
        assert "'Enable'" in out.stdout
        assert str(ol_dir) in out.stdout and str(locale) in out.stdout

    def test_placeholder_defect_alone_fails(self, tmp_path):
        entry = 'msgid "by %(name)s"\nmsgstr "par"'
        ol_dir, locale = _write_pair(tmp_path, ol={"fr": [ENABLE]},
                                     baked={"fr": [ENABLE, entry]})
        out = _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", "-")
        assert out.returncode == 1
        assert json.loads(out.stdout)["totals"]["placeholder_defects"] == 1

    def test_baseline_fails_only_on_new_findings(self, tmp_path):
        ol_dir, locale = _write_pair(tmp_path, ol={"es": [ENABLE]}, baked={"es": [ENABLE_FUZZY]})
        base = tmp_path / "base.json"
        assert _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale,
                    "--json", base).returncode == 1
        same = _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--baseline", base)
        assert same.returncode == 0, same.stdout
        assert "not in baseline:        0" in same.stdout

        entry = 'msgid "by %(name)s"\nmsgstr "por"'
        (locale / "es" / "messages.po").write_text(_po_text(ENABLE_FUZZY, entry))
        (ol_dir / "messages.pot").write_text(
            _po_text('msgid "Enable"\nmsgstr ""', 'msgid "by %(name)s"\nmsgstr ""'))
        worse = _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--baseline", base)
        assert worse.returncode == 1
        assert "not in baseline:        1" in worse.stdout

    def test_locale_without_openlibrary_directory_is_installed_as_new(self, tmp_path):
        # openlibrary#13070's install_translations() creates the directory.
        ol_dir, locale = _write_pair(tmp_path, ol={"es": [ENABLE]},
                                     baked={"es": [ENABLE], "zz": [ENABLE]})
        out = _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", "-")
        assert out.returncode == 0, out.stdout
        [f] = [f for f in json.loads(out.stdout)["findings"] if f["lang"] == "zz"]
        assert f["kind"] == "new_locale" and f["gating"] is False

    def test_openlibrary_only_locale_is_reported_not_counted(self, tmp_path):
        ol_dir, locale = _write_pair(tmp_path, ol={"es": [ENABLE], "az": [ENABLE]},
                                     baked={"es": [ENABLE]})
        out = _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", "-")
        assert out.returncode == 0
        assert json.loads(out.stdout)["not_baked"] == ["az"]

    @pytest.mark.parametrize("breakage", ["no_pot", "no_translations", "no_locales"])
    def test_a_check_that_cannot_run_exits_2_not_0(self, tmp_path, breakage):
        ol_dir, locale = _write_pair(tmp_path, ol={"es": [ENABLE]}, baked={"es": [ENABLE]})
        if breakage == "no_pot":
            (ol_dir / "messages.pot").unlink()
        elif breakage == "no_translations":
            (ol_dir / "es" / "messages.po").write_text(_po_text('msgid "Enable"\nmsgstr ""'))
        else:
            (locale / "es" / "messages.po").unlink()
        out = _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale)
        assert out.returncode == 2, (out.stdout, out.stderr)
        assert "did not run" in out.stderr

    def test_git_ref_source_reads_committed_locale(self):
        source = br.GitRefSource("HEAD", _ROOT)
        langs = source.languages()
        assert "es" in langs
        assert source.fetch("es/messages.po").startswith(b"#")

    def test_i18n_subcommand_delegates(self, tmp_path):
        ol_dir, locale = _write_pair(tmp_path, ol={"es": [ENABLE]}, baked={"es": [ENABLE_FUZZY]})
        out = subprocess.run(
            [sys.executable, str(_ROOT / "i18n"), "bake-regression",
             "--openlibrary-dir", str(ol_dir), "--locale-dir", str(locale)],
            capture_output=True, text=True, cwd=_ROOT)
        assert out.returncode == 1, out.stderr
        assert "regressions to English: 1" in out.stdout


# ---------------------------------------------------------------------------
# Ways a real regression could read as a pass (adversarial review of #104)
# ---------------------------------------------------------------------------

FR_FORMS = "nplurals=2; plural=(n > 1);"


def _write(root: Path, lang: str, entries: list[str], plural_forms=TWO_FORMS, language="de"):
    (root / lang).mkdir(parents=True, exist_ok=True)
    (root / lang / "messages.po").write_text(
        _po_text(*entries, plural_forms=plural_forms, language=language))


def _pot(ol_dir: Path, *entries: str):
    ol_dir.mkdir(parents=True, exist_ok=True)
    (ol_dir / "messages.pot").write_text(_po_text(*entries))


def _run(ol_dir, locale, *extra):
    out = _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", "-", *extra)
    report = json.loads(out.stdout) if out.returncode in (0, 1) else None
    return out.returncode, report, out


DAYS_POT = DAYS_ID + '\nmsgstr[0] ""\nmsgstr[1] ""'


class TestBaselineComparesWhatIsWrong:
    def _pl(self, tmp_path, base_entry, head_entry):
        ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
        _pot(ol_dir, WAITING_ID + '\nmsgstr[0] ""\nmsgstr[1] ""')
        _write(ol_dir, "pl", [WAITING_OL], plural_forms=None)
        _write(locale, "pl", [base_entry], plural_forms=PL_FORMS, language="pl")
        base = tmp_path / "base.json"
        _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", base)
        _write(locale, "pl", [head_entry], plural_forms=PL_FORMS, language="pl")
        return _run(ol_dir, locale, "--baseline", base)

    def test_more_forms_going_english_on_a_regressing_entry_is_new(self, tmp_path):
        code, report, out = self._pl(tmp_path, WAITING_PL_EMPTY_3RD, "#, fuzzy\n" + WAITING_PL_FULL)
        assert code == 1, out.stdout
        assert report["totals"]["new_findings"] == 1

    def test_fewer_forms_going_english_is_not_new(self, tmp_path):
        emptier = WAITING_ID + '\nmsgstr[0] "Czeka %(count)d osoba na tę książkę."\nmsgstr[1] ""\nmsgstr[2] ""'
        code, report, out = self._pl(tmp_path, emptier, WAITING_PL_EMPTY_3RD)
        assert code == 0, out.stdout

    def _ru(self, tmp_path, base_forms, head_forms):
        ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
        # openlibrary leaves the plural untranslated; "Enable" keeps its side live.
        _pot(ol_dir, DAYS_POT, ENABLE)
        _write(ol_dir, "ru", [DAYS_POT, ENABLE], plural_forms=RU_FORMS, language="ru")
        entry = lambda forms: DAYS_ID + "".join(f'\nmsgstr[{i}] "{f}"' for i, f in enumerate(forms))
        _write(locale, "ru", [entry(base_forms), ENABLE], plural_forms=RU_FORMS, language="ru")
        base = tmp_path / "base.json"
        _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", base)
        _write(locale, "ru", [entry(head_forms), ENABLE], plural_forms=RU_FORMS, language="ru")
        return _run(ol_dir, locale, "--baseline", base)

    def test_second_placeholder_defect_on_an_entry_with_one_is_new(self, tmp_path):
        good = ["Ожидает 1 день", "Ожидает %(count)d дня", "Ожидает %(count)d дней"]
        worse = ["Ожидает 1 день", "Ожидает %(count)d дня", "Ожидает много дней"]
        code, report, out = self._ru(tmp_path, good, worse)
        assert code == 1, out.stdout
        assert report["totals"]["new_findings"] == 1

    def test_partly_fixed_placeholder_defect_is_not_new(self, tmp_path):
        bad = ["Ожидает 1 день", "Ожидает %(count)d дня", "Ожидает много дней"]
        better = ["Ожидает 1 день", "Ожидает %(count)d дня", "Ожидает %(count)d дней"]
        code, report, out = self._ru(tmp_path, bad, better)
        assert code == 0, out.stdout

    def test_same_msgid_in_another_msgctxt_is_a_different_finding(self, tmp_path):
        ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
        a = 'msgctxt "a"\nmsgid "Open"\nmsgstr "Abrir"'
        b = 'msgctxt "b"\nmsgid "Open"\nmsgstr "Abierto"'
        _pot(ol_dir, a, b)
        _write(ol_dir, "es", [a, b])
        _write(locale, "es", ["#, fuzzy\n" + a, b])
        base = tmp_path / "base.json"
        _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", base)
        _write(locale, "es", ["#, fuzzy\n" + a, "#, fuzzy\n" + b])
        code, report, out = _run(ol_dir, locale, "--baseline", base)
        assert code == 1, out.stdout
        assert report["totals"]["new_findings"] == 1


class TestAlreadyShippedComparesWhatIsWrong:
    def test_defect_beyond_what_openlibrary_ships_gates(self, tmp_path):
        ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
        _pot(ol_dir, DAYS_POT)
        # openlibrary shows "1 jour" at n=0: a defect it already ships.
        ol_entry = DAYS_ID + '\nmsgstr[0] "1 jour"\nmsgstr[1] "%(count)d jours"'
        _write(ol_dir, "fr", [ol_entry], plural_forms=FR_FORMS, language="fr")
        # The bake keeps that and also drops the count for n >= 2.
        baked = DAYS_ID + '\nmsgstr[0] "1 jour"\nmsgstr[1] "des jours"'
        _write(locale, "fr", [baked], plural_forms=FR_FORMS, language="fr")
        code, report, out = _run(ol_dir, locale)
        assert code == 1, out.stdout
        assert report["totals"]["placeholder_defects_new"] == 1

    def test_same_defect_openlibrary_ships_does_not_gate(self, tmp_path):
        ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
        _pot(ol_dir, DAYS_POT)
        entry = DAYS_ID + '\nmsgstr[0] "1 jour"\nmsgstr[1] "%(count)d jours"'
        _write(ol_dir, "fr", [entry], plural_forms=FR_FORMS, language="fr")
        _write(locale, "fr", [entry], plural_forms=FR_FORMS, language="fr")
        code, report, out = _run(ol_dir, locale)
        assert code == 0, out.stdout
        assert report["totals"]["placeholder_defects"] == 1

    def test_openlibrary_defect_on_another_entry_does_not_excuse_this_one(self, tmp_path):
        ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
        a = 'msgid "by %(name)s"\nmsgstr "par"'
        b_good = 'msgid "for %(name)s"\nmsgstr "pour %(name)s"'
        b_bad = 'msgid "for %(name)s"\nmsgstr "pour"'
        _pot(ol_dir, a, b_good)
        _write(ol_dir, "fr", [a, b_good])
        _write(locale, "fr", [a, b_bad])
        code, report, out = _run(ol_dir, locale)
        assert code == 1, out.stdout
        assert report["totals"]["placeholder_defects_new"] == 1


class TestRendersEnglish:
    def test_msgstr_that_is_the_english_text_is_a_regression(self, tmp_path):
        ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
        _pot(ol_dir, 'msgid "Borrow"\nmsgstr ""')
        _write(ol_dir, "es", ['msgid "Borrow"\nmsgstr "Prestar"'])
        _write(locale, "es", ['msgid "Borrow"\nmsgstr "Borrow"'])
        code, report, out = _run(ol_dir, locale)
        assert code == 1, out.stdout
        assert [f["reason"] for f in report["findings"]] == ["english-copy"]

    def test_plural_msgid_in_context_is_looked_up_in_context(self):
        # The baked side has the plural only without the context openlibrary asks for.
        ctx_entry = 'msgctxt "queue"\n' + WAITING_OL
        live = _live('msgctxt "queue"\n' + WAITING_ID + '\nmsgstr[0] ""\nmsgstr[1] ""')
        [f] = _regressions(_cat(ctx_entry), _cat(WAITING_OL), live)
        assert f.msgctxt == "queue"

    def test_singular_in_context_reports_the_baked_entry_in_context(self):
        ol = _cat('msgctxt "button"\nmsgid "Enable"\nmsgstr "Activar"')
        baked = _cat('msgctxt "button"\n' + ENABLE_FUZZY, ENABLE)
        live = _live('msgctxt "button"\nmsgid "Enable"\nmsgstr ""')
        [f] = _regressions(ol, baked, live)
        assert f.reason == "fuzzy" and f.baked_msgstr == "Ejemplo"

    def test_form_only_the_baked_rule_selects_is_probed(self):
        # openlibrary: 2 forms; baked: 3 forms whose middle form is empty.
        live = _live(WAITING_ID + '\nmsgstr[0] ""\nmsgstr[1] ""')
        baked = WAITING_ID + ('\nmsgstr[0] "Czeka %(count)d osoba na tę książkę."\nmsgstr[1] ""'
                              '\nmsgstr[2] "Czeka %(count)d osób na tę książkę."')
        [f] = _regressions(_cat(WAITING_OL, plural_forms=None),
                           _cat(baked, plural_forms=PL_FORMS, language="pl"), live)
        assert 2 in f.n and 5 not in f.n


class TestBakeRejection:
    """openlibrary#13070 keeps openlibrary's file for a locale whose .po would crash a render."""

    CRASH = 'msgid "by %(name)s"\nmsgstr "par %(nom)s"'

    def test_render_crash_rejects_the_locale_and_gates(self, tmp_path):
        ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
        _pot(ol_dir, ENABLE, 'msgid "by %(name)s"\nmsgstr ""')
        _write(ol_dir, "fr", [ENABLE, self.CRASH])
        _write(locale, "fr", [ENABLE, self.CRASH])
        # openlibrary ships the very same msgstr, so it is not a new placeholder
        # defect; the new harm is that this repo's whole fr is not baked.
        code, report, out = _run(ol_dir, locale)
        assert code == 1, out.stdout
        assert report["languages"]["fr"]["rejected"] is True
        assert report["totals"]["rejected_locales"] == 1

    def test_crash_on_an_entry_openlibrary_ships_with_a_lesser_defect(self, tmp_path):
        ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
        _pot(ol_dir, DAYS_POT)
        _write(ol_dir, "fr", [DAYS_ID + '\nmsgstr[0] "1 jour"\nmsgstr[1] "%(count)d jours"'],
               plural_forms=FR_FORMS, language="fr")
        _write(locale, "fr", [DAYS_ID + '\nmsgstr[0] "1 jour"\nmsgstr[1] "%(nombre)d jours"'],
               plural_forms=FR_FORMS, language="fr")
        code, report, out = _run(ol_dir, locale)
        assert code == 1, out.stdout

    def test_fuzzy_crash_does_not_reject(self):
        assert br.install_rejections(_po_text("#, fuzzy\n" + self.CRASH).encode()) == []
        assert br.install_rejections(_po_text(self.CRASH).encode())

    def test_unparseable_file_is_a_rejection_not_an_error(self, tmp_path):
        ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
        _pot(ol_dir, ENABLE)
        _write(ol_dir, "es", [ENABLE])
        (locale / "es").mkdir(parents=True)
        (locale / "es" / "messages.po").write_text(_po_text('msgid "a"\nthis is not po\nmsgstr "b"'))
        code, report, out = _run(ol_dir, locale)
        assert code == 1, (out.stdout, out.stderr)
        assert report["languages"]["es"]["rejected"] is True


class TestNotLiveIsAnError:
    def test_one_empty_openlibrary_language_is_an_error(self, tmp_path):
        ol_dir, locale = tmp_path / "ol", tmp_path / "locale"
        _pot(ol_dir, ENABLE)
        _write(ol_dir, "es", [ENABLE])
        (ol_dir / "de").mkdir()
        (ol_dir / "de" / "messages.po").write_bytes(b"")
        _write(locale, "es", [ENABLE])
        _write(locale, "de", ["#, fuzzy\n" + ENABLE])
        code, _, out = _run(ol_dir, locale)
        assert code == 2, out.stdout

    def test_baseline_against_another_openlibrary_is_an_error(self, tmp_path):
        ol_dir, locale = _write_pair(tmp_path, ol={"es": [ENABLE]}, baked={"es": [ENABLE_FUZZY]})
        base = tmp_path / "base.json"
        _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", base)
        other = tmp_path / "ol2"
        other.mkdir()
        (other / "messages.pot").write_text((ol_dir / "messages.pot").read_text())
        _write(other, "es", [ENABLE])
        code, _, out = _run(other, locale, "--baseline", base)
        assert code == 2, out.stdout

    def test_openlibrary_side_that_changed_under_the_baseline_is_an_error(self, tmp_path):
        ol_dir, locale = _write_pair(tmp_path, ol={"es": [ENABLE, 'msgid "Deploy"\nmsgstr "Implementar"']},
                                     baked={"es": [ENABLE_FUZZY]})
        base = tmp_path / "base.json"
        _cli("--openlibrary-dir", ol_dir, "--locale-dir", locale, "--json", base)
        _write(ol_dir, "es", [ENABLE])
        code, _, out = _run(ol_dir, locale, "--baseline", base)
        assert code == 2, out.stdout
