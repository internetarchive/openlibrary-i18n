"""Tests for scripts/port_from_openlibrary.py (#99).

In-memory catalogs throughout, except TestTextOrigin, which builds a throwaway git repo
shaped like openlibrary's so the history walk runs against real git.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from io import BytesIO
from pathlib import Path

import pytest
from babel.messages.pofile import read_po, write_po

_ROOT = Path(__file__).parent.parent
_path = _ROOT / "scripts" / "port_from_openlibrary.py"
_spec = importlib.util.spec_from_file_location(
    "port_from_openlibrary", _path, loader=SourceFileLoader("port_from_openlibrary", str(_path)))
port = importlib.util.module_from_spec(_spec)
sys.modules["port_from_openlibrary"] = port  # dataclasses resolve their module by name
_spec.loader.exec_module(port)


def _header(plurals: str = "nplurals=2; plural=(n != 1);") -> str:
    return ('msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
            f'"Plural-Forms: {plurals}\\n"\n\n')


def _cat(*entries: str, plurals: str = "nplurals=2; plural=(n != 1);", locale: str = "es"):
    po = _header(plurals) + "\n\n".join(entries) + "\n"
    return read_po(BytesIO(po.encode()), locale=locale)


def _live(*cats):
    return {port.entry_key(m) for c in cats for m in c if m.id}


def _origin(verdict):
    return lambda key: port.Origin("f" * 40, verdict, "test")


def _port(here, ol, verdict="ai", lang="es"):
    return {r.msgid: r for r in port.port_catalog(lang, here, ol, _live(here), _origin(verdict))}


def _get(cat, msgid):
    return next(m for m in cat if m.id and port.entry_key(m)[1] == msgid)


# ---------------------------------------------------------------------------
# classify_entry
# ---------------------------------------------------------------------------

class TestClassifyEntry:
    @pytest.mark.parametrize(("here", "ol", "cls"), [
        ('msgid "A"\nmsgstr "a"', None, "only_here"),
        ('msgid "A"\nmsgstr "a"', 'msgid "A"\nmsgstr ""', "only_here"),
        ('msgid "A"\nmsgstr "a"', 'msgid "A"\nmsgstr "a"', "same"),
        ('msgid "A"\nmsgstr ""', '#, fuzzy\nmsgid "A"\nmsgstr "x"', "ol_fuzzy"),
        ('msgid "A"\nmsgstr ""', 'msgid "A"\nmsgstr "a"', "copy_empty"),
        ('#, fuzzy\nmsgid "A"\nmsgstr "x"', 'msgid "A"\nmsgstr "a"', "copy_fuzzy"),
        ('msgid "A"\nmsgstr "b"', 'msgid "A"\nmsgstr "a"', "conflict"),
    ])
    def test_classes(self, here, ol, cls):
        h = _get(_cat(here), "A")
        o = _get(_cat(ol), "A") if ol else None
        assert port.classify_entry(h, o) == cls

    def test_fuzzy_openlibrary_is_ol_fuzzy_even_with_other_arity(self):
        three = "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 ? 1 : 2);"
        here = _cat('msgid "a"\nmsgid_plural "as"\nmsgstr[0] "x"\nmsgstr[1] "y"\nmsgstr[2] ""',
                    plurals=three, locale="pl")
        ol = _cat('#, fuzzy\nmsgid "a"\nmsgid_plural "as"\nmsgstr[0] "X"\nmsgstr[1] "Y"', locale="pl")
        assert port.classify_entry(_get(here, "a"), _get(ol, "a")) == "ol_fuzzy"

    def test_plural_arity_mismatch_is_its_own_class(self):
        three = "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 ? 1 : 2);"
        here = _cat('msgid "a"\nmsgid_plural "as"\nmsgstr[0] "x"\nmsgstr[1] "y"\nmsgstr[2] ""',
                    plurals=three, locale="pl")
        ol = _cat('msgid "a"\nmsgid_plural "as"\nmsgstr[0] "x"\nmsgstr[1] "y"', locale="pl")
        assert port.classify_entry(_get(here, "a"), _get(ol, "a")) == "plural_arity"


# ---------------------------------------------------------------------------
# classify_commit / decide
# ---------------------------------------------------------------------------

class TestClassifyCommit:
    @pytest.mark.parametrize(("name", "email", "message", "locales", "verdict"), [
        # the June 2026 openlibrary pipeline commits
        ("Michael E. Karpeles (Mek)", "michael.karpeles@gmail.com",
         "i18n(es): AI translation update — sync with messages.pot (2026-06-10)", 1, "ai"),
        ("Mek", "mek@archive.org", "i18n(uk): fix format-string errors — clear 1 mismatched fuzzy msgstr", 1, "ai"),
        ("Michael E. Karpeles (Mek)", "m@x", "i18n(te): batch 3 — AI translations (claude-sonnet-4-6)", 1, "ai"),
        ("Claude Sonnet 5", "noreply@anthropic.com", "Switch reorder-only zh/ko translations", 2, "ai"),
        ("Mek", "m@x", "i18n(tr/cs): sync with messages.pot", 2, "ai"),
        ("Drini Cami", "d@x", "Fix az\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)", 1, "ai"),
        # human translators (openlibrary #13320, #13369, #12552, #12830)
        ("Daniel Capilla", "dcapillae@x", "Update Spanish translation (#13320)", 1, "human"),
        ("dcapillae", "dcapillae@x", "Update Spanish translation", 1, "human"),
        ("Milo Ivir", "m@x", "Update Croatian translation (#12552)", 1, "human"),
        ("systile", "s@x", "Adding initial Korean translation (#12830)", 1, "human"),
        # a human-authored bulk msgmerge wrote nothing itself
        ("Tom Morris", "t@x", "Update all .po files from messages.pot", 20, "indeterminate"),
    ])
    def test_verdicts(self, name, email, message, locales, verdict):
        assert port.classify_commit(name, email, message, locales)[0] == verdict


class TestDecide:
    @pytest.mark.parametrize(("cls", "prov", "action"), [
        ("copy_empty", None, "copy"), ("copy_fuzzy", None, "copy"),
        ("conflict", "human", "copy"), ("conflict", "ai", "keep"),
        ("conflict", "indeterminate", "keep"),
        ("ol_fuzzy", None, "keep"), ("plural_arity", None, "keep"),
        ("only_here", None, "keep"), ("same", None, "keep"),
    ])
    def test_table(self, cls, prov, action):
        assert port.decide(cls, prov) == action


class TestNormalize:
    def test_whitespace_ignored(self):
        assert port.normalize_text(("<b>x</b>  y ",)) == port.normalize_text(("<b>x</b> y",))

    def test_lint_marker_is_not_ignored(self):
        assert port.normalize_text(("<!-- i18n-lint no-tree-order --><b>x</b>",)) != \
            port.normalize_text(("<!-- i18n-lint no-tree-equal --><b>x</b>",))

    def test_text_change_is_not_ignored(self):
        assert port.normalize_text(("Activar",)) != port.normalize_text(("Ejemplo",))


# ---------------------------------------------------------------------------
# port_catalog
# ---------------------------------------------------------------------------

class TestPortCatalog:
    def test_fuzzy_here_is_replaced_and_made_non_fuzzy(self):
        here = _cat('#, fuzzy\nmsgid "Enable"\nmsgstr "Ejemplo"')
        _get(here, "Enable").previous_id = ["Example"]  # Babel 2.18's read_po drops #| lines
        ol = _cat('msgid "Enable"\nmsgstr "Activar"')
        rows = _port(here, ol)
        m = _get(here, "Enable")
        assert rows["Enable"].action == "copy"
        assert (m.string, m.fuzzy, m.previous_id) == ("Activar", False, [])

    def test_openlibrary_fuzzy_is_never_copied(self):
        here = _cat('msgid "A"\nmsgstr ""', '#, fuzzy\nmsgid "B"\nmsgstr "old"')
        ol = _cat('#, fuzzy\nmsgid "A"\nmsgstr "x"', '#, fuzzy\nmsgid "B"\nmsgstr "y"')
        _port(here, ol, verdict="human")
        a, b = _get(here, "A"), _get(here, "B")
        assert (a.string, a.fuzzy) == ("", False)
        assert (b.string, b.fuzzy) == ("old", True)

    def test_conflict_follows_provenance(self):
        for verdict, want in (("human", "humano"), ("ai", "máquina"), ("indeterminate", "máquina")):
            here = _cat('msgid "A"\nmsgstr "máquina"')
            ol = _cat('msgid "A"\nmsgstr "humano"')
            _port(here, ol, verdict=verdict)
            assert _get(here, "A").string == want, verdict

    def test_origin_is_only_consulted_for_conflicts(self):
        here = _cat('msgid "A"\nmsgstr ""', 'msgid "B"\nmsgstr "b"')
        ol = _cat('msgid "A"\nmsgstr "a"', 'msgid "B"\nmsgstr "b"')
        asked = []
        port.port_catalog("es", here, ol, _live(here), lambda k: asked.append(k))
        assert asked == []

    def test_retired_openlibrary_strings_are_not_added(self):
        here = _cat('msgid "A"\nmsgstr "a"')
        ol = _cat('msgid "A"\nmsgstr "a"', 'msgid "Gone"\nmsgstr "ido"')
        _port(here, ol)
        assert "Gone" not in {m.id for m in here}

    def test_entries_outside_the_pot_are_untouched(self):
        here = _cat('msgid "A"\nmsgstr ""')
        ol = _cat('msgid "A"\nmsgstr "a"')
        assert port.port_catalog("es", here, ol, set(), _origin("human")) == []
        assert _get(here, "A").string == ""

    def test_plural_arity_is_left_alone(self):
        three = "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 ? 1 : 2);"
        here = _cat('msgid "a"\nmsgid_plural "as"\nmsgstr[0] "x"\nmsgstr[1] "y"\nmsgstr[2] ""',
                    plurals=three, locale="pl")
        ol = _cat('msgid "a"\nmsgid_plural "as"\nmsgstr[0] "X"\nmsgstr[1] "Y"', locale="pl")
        _port(here, ol, verdict="human", lang="pl")
        assert _get(here, "a").string == ("x", "y", "")

    def test_placeholder_mismatch_is_excluded(self):
        # openlibrary hr, lists/preview.html: positional msgid, named msgstr
        entry = '#, python-format\nmsgid "by <a href=\\"%s\\">You</a>"\nmsgstr "{}"'
        here = _cat(entry.format('autor: <a href=\\"%s\\">vi</a>'), locale="hr")
        ol = _cat(entry.format('autor: <a href=\\"%(link)s\\">ti</a>'), locale="hr")
        rows = _port(here, ol, verdict="human", lang="hr")
        r = rows['by <a href="%s">You</a>']
        assert r.action == "excluded" and "placeholder mismatch" in r.excluded
        assert _get(here, 'by <a href="%s">You</a>').string == 'autor: <a href="%s">vi</a>'

    def test_html_structure_failure_is_excluded(self):
        here = _cat('msgid "<b>A</b> <i>B</i>"\nmsgstr ""')
        ol = _cat('msgid "<b>A</b> <i>B</i>"\nmsgstr "<i>B</i> <b>A</b>"')
        rows = _port(here, ol)
        assert rows["<b>A</b> <i>B</i>"].action == "excluded"
        assert any("test_html_format" in e for e in rows["<b>A</b> <i>B</i>"].excluded)
        assert _get(here, "<b>A</b> <i>B</i>").string == ""

    def test_entry_the_daily_fix_would_clear_is_excluded(self, monkeypatch):
        # Whatever the real ./i18n fix step clears is excluded. The step is replaced on the
        # loaded toolbox, so this holds as the toolbox's rules change (issue #101 changes
        # mismatch()); the port reaches it by the same getattr as in production.
        def clears_one_book(catalog):
            for m in catalog:
                if m.id == "Hi %(name)s":
                    m.string = ""
            return ["Hi %(name)s"]
        monkeypatch.setattr(port.toolbox(), "_fix_format_errors", clears_one_book)
        here = _cat('#, python-format\nmsgid "Hi %(name)s"\nmsgstr ""')
        ol = _cat('#, python-format\nmsgid "Hi %(name)s"\nmsgstr "Hola %(name)s"')
        rows = _port(here, ol)
        assert rows["Hi %(name)s"].action == "excluded"
        assert rows["Hi %(name)s"].excluded == ["./i18n fix: _fix_format_errors"]
        assert _get(here, "Hi %(name)s").string == ""

    def test_later_plural_form_dropping_the_count_is_excluded(self):
        # Issue #15's defect. Babel's checker allows omissions and ./i18n fix compares
        # against the singular, so only the per-form placeholder check sees it.
        entry = 'msgid "One book"\nmsgid_plural "%(n)d books"\nmsgstr[0] "{}"\nmsgstr[1] "{}"'
        here = _cat(entry.format("", ""))
        ol = _cat(entry.format("Un libro", "libros"))
        r = _port(here, ol)["One book"]
        assert (r.action, r.excluded) == ("excluded", ["placeholder mismatch"])

    def test_only_form_of_a_one_plural_language_must_carry_the_count(self):
        # nplurals=1 (ko, ja, id): form 0 serves every n, so it must follow msgid_plural.
        # openlibrary's ko "%(name)s has 1 list." is the real case (issue #15's defect).
        entry = 'msgid "%(name)s has 1 list."\nmsgid_plural "%(name)s has %(count)s lists."\nmsgstr[0] "{}"'
        here = _cat(entry.format("%(name)s님의 목록 %(count)s개"), plurals="nplurals=1; plural=0;", locale="ko")
        ol = _cat(entry.format("%(name)s님은 목록이 1개 있습니다."), plurals="nplurals=1; plural=0;", locale="ko")
        r = _port(here, ol, verdict="human", lang="ko")["%(name)s has 1 list."]
        assert r.action == "excluded" and "placeholder mismatch" in r.excluded

    def test_plural_form_zero_may_follow_either_msgid(self):
        entry = 'msgid "One book"\nmsgid_plural "%(n)d books"\nmsgstr[0] "{}"\nmsgstr[1] "{}"'
        for zero in ("Un libro", "%(n)d libro"):
            here = _cat(entry.format("", ""), locale="hr")
            ol = _cat(entry.format(zero, "%(n)d knjiga"), locale="hr")
            r = _port(here, ol, lang="hr")["One book"]
            assert "placeholder mismatch" not in r.excluded, zero

    def test_validator_failure_is_reported_as_the_ci_gate(self, monkeypatch):
        # validate-pr.yml runs tests/validators.py directly, so a copy it rejects is reported
        # as failing CI, whichever rule rejects it.
        real = port.validators().validate
        monkeypatch.setattr(port.validators(), "validate",
                            lambda m, c: ["    rejected"] if m.id == "Hi %(name)s" else real(m, c))
        here = _cat('#, python-format\nmsgid "Hi %(name)s"\nmsgstr ""')
        ol = _cat('#, python-format\nmsgid "Hi %(name)s"\nmsgstr "Hola %(name)s"')
        r = _port(here, ol)["Hi %(name)s"]
        assert (r.action, r.excluded) == ("excluded", ["validators: rejected"])

    def test_msgstr_identical_to_the_msgid_is_excluded(self):
        # English left in: copying it would mark the entry done and keep the AI run off it.
        here = _cat('#, fuzzy\nmsgid "Add a new series"\nmsgstr "Añadir una nueva lista"')
        ol = _cat('msgid "Add a new series"\nmsgstr "Add a new series"')
        r = _port(here, ol)["Add a new series"]
        assert (r.action, r.excluded) == ("excluded", ["msgstr identical to msgid"])
        assert _get(here, "Add a new series").fuzzy

    @pytest.mark.parametrize("name", ["PDA", "Bluesky", "PR", "Open Library", "LibriVox",
                                      "Bookshop.org", "Inventaire.io:"])
    def test_a_name_identical_to_its_msgid_is_copied(self, name):
        here = _cat(f'#, fuzzy\nmsgid "{name}"\nmsgstr "Actualizar"')
        ol = _cat(f'msgid "{name}"\nmsgstr "{name}"')
        assert _port(here, ol)[name].action == "copy"

    def test_fix_alters_sees_a_flag_only_change(self):
        cat = _cat('#, fuzzy\nmsgid "a"\nmsgid_plural "as"\nmsgstr[0] ""\nmsgstr[1] ""')
        assert port.fix_alters(cat, {(None, "a")}) == {(None, "a"): "_fix_format_errors"}

    def test_clean_copy_is_not_excluded(self):
        here = _cat('#, python-format\nmsgid "Hi %(name)s"\nmsgstr ""')
        ol = _cat('#, python-format\nmsgid "Hi %(name)s"\nmsgstr "Hola %(name)s"')
        assert _port(here, ol)["Hi %(name)s"].action == "copy"


# ---------------------------------------------------------------------------
# retired / near_matches / wrap_width
# ---------------------------------------------------------------------------

class TestRetired:
    def test_counts_only_translated_msgids_outside_openlibrarys_pot(self):
        ol = _cat('msgid "Live"\nmsgstr "x"', 'msgid "Gone"\nmsgstr "y"', 'msgid "Empty"\nmsgstr ""')
        assert port.retired(ol, {(None, "Live")}) == ["Gone"]

    def test_near_matches(self):
        hits = port.near_matches(["Borrow this book now", "Totally unrelated"],
                                 ["Borrow this book", "Search"])
        assert hits == {"Borrow this book now": "Borrow this book"}


class TestWrapWidth:
    @pytest.mark.parametrize("width", port.WRAP_WIDTHS)
    def test_file_keeps_its_wrap(self, width):
        cat = _cat('msgid "' + "long words " * 12 + '"\nmsgstr "' + "palabras largas " * 12 + '"')
        buf = BytesIO()
        write_po(buf, cat, width=width, omit_header=False)
        raw = buf.getvalue()
        assert port.wrap_width(raw, read_po(BytesIO(raw))) == width


# ---------------------------------------------------------------------------
# text_origin, against real git
# ---------------------------------------------------------------------------

def _git(repo, *args, name="Someone", email="s@x"):
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
           "GIT_COMMITTER_EMAIL": email, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "HOME": str(repo)}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                          capture_output=True, text=True).stdout


class TestTextOrigin:
    @pytest.fixture
    def repo(self, tmp_path):
        _git(tmp_path, "init", "-q", "-b", "master")
        (tmp_path / "openlibrary/i18n/es").mkdir(parents=True)
        return tmp_path

    def _commit(self, repo, po_body, message, name="Someone", email="s@x"):
        (repo / "openlibrary/i18n/es/messages.po").write_text(_header() + po_body + "\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", message, name=name, email=email)
        return _git(repo, "rev-parse", "HEAD").strip()

    def test_human_translation_survives_a_later_rewrap_and_refactor(self, repo):
        human = self._commit(repo, '#: a.html\nmsgid "A"\nmsgstr "Activar ahora"',
                             "Update Spanish translation (#13320)", name="Daniel Capilla")
        self._commit(repo, '#: a.html.jinja\nmsgid "A"\nmsgstr ""\n"Activar "\n"ahora"',
                     "refactor: convert templates to Jinja", name="A Developer")
        o = port.OpenLibraryRepo(repo, "master").text_origin("es", (None, "A"))
        assert (o.sha, o.verdict) == (human, "human")
        assert len(o.looked_past) == 1

    def test_lint_marker_change_is_authorship(self, repo):
        # The marker is part of what a port would copy, so whoever wrote it is the author
        # of the difference, even when the words are someone else's.
        self._commit(repo, 'msgid "A"\nmsgstr "<!-- i18n-lint no-tree-equal --><b>x</b>"',
                     "Adding initial Korean translation (#12830)", name="systile")
        ai = self._commit(repo, 'msgid "A"\nmsgstr "<!-- i18n-lint no-tree-order --><b>x</b>"',
                          "Switch markers", name="Claude Sonnet 5", email="noreply@anthropic.com")
        o = port.OpenLibraryRepo(repo, "master").text_origin("es", (None, "A"))
        assert (o.sha, o.verdict) == (ai, "ai")

    def test_same_text_on_an_unrelated_msgid_is_not_followed(self, repo):
        self._commit(repo, 'msgid "Preview Only"\nmsgstr "Vista previa"', "Update Spanish", name="dcapillae")
        ai = self._commit(repo, 'msgid "Preview Only"\nmsgstr "Vista previa"\n\nmsgid "Book Preview"\nmsgstr "Vista previa"',
                          "i18n(es): AI translation update", name="Mek")
        o = port.OpenLibraryRepo(repo, "master").text_origin("es", (None, "Book Preview"))
        assert (o.sha, o.verdict) == (ai, "ai")

    def test_merge_is_attributed_to_the_branch_commit(self, repo):
        self._commit(repo, 'msgid "A"\nmsgstr "uno"\n\nmsgid "B"\nmsgstr "dos"', "base", name="Base")
        _git(repo, "checkout", "-q", "-b", "es")
        ai = self._commit(repo, 'msgid "A"\nmsgstr "UNO"\n\nmsgid "B"\nmsgstr "dos"', "Tweak",
                          name="Claude", email="noreply@anthropic.com")
        _git(repo, "checkout", "-q", "master")
        self._commit(repo, 'msgid "A"\nmsgstr "uno"\n\nmsgid "B"\nmsgstr "DOS"', "Other change", name="Base")
        try:  # the two sides touch neighbouring lines, so git stops for a resolution
            _git(repo, "merge", "-q", "--no-commit", "es", name="Maintainer")
        except subprocess.CalledProcessError:
            pass
        (repo / "openlibrary/i18n/es/messages.po").write_text(
            _header() + 'msgid "A"\nmsgstr "UNO"\n\nmsgid "B"\nmsgstr "DOS"\n')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "Merge pull request #1 from someone/es", name="Maintainer")
        o = port.OpenLibraryRepo(repo, "master").text_origin("es", (None, "A"))
        assert (o.sha, o.verdict) == (ai, "ai")

    def test_no_cap_on_commits_that_left_the_entry_alone(self, repo):
        human = self._commit(repo, 'msgid "A"\nmsgstr "a"', "Update Spanish", name="dcapillae")
        for i in range(35):  # more than the 30-commit cap an earlier version had
            self._commit(repo, f'msgid "A"\nmsgstr "a"\n\nmsgid "N{i}"\nmsgstr "n"', f"filler {i}", name="Dev")
        o = port.OpenLibraryRepo(repo, "master").text_origin("es", (None, "A"))
        assert (o.sha, o.verdict) == (human, "human")

    def test_ai_rewrite_of_human_text_is_ai(self, repo):
        self._commit(repo, 'msgid "A"\nmsgstr "Activar"', "Update Spanish translation", name="dcapillae")
        ai = self._commit(repo, 'msgid "A"\nmsgstr "Habilitar"',
                          "i18n(es): AI translation update — sync with messages.pot", name="Mek")
        o = port.OpenLibraryRepo(repo, "master").text_origin("es", (None, "A"))
        assert (o.sha, o.verdict, o.looked_past) == (ai, "ai", [])

    def test_unfuzzying_is_authorship(self, repo):
        self._commit(repo, '#, fuzzy\nmsgid "A"\nmsgstr "Ejemplo"', "Update .po", name="Tom Morris")
        ai = self._commit(repo, 'msgid "A"\nmsgstr "Ejemplo"', "i18n(es): batch 1 — AI translations",
                          name="Mek")
        o = port.OpenLibraryRepo(repo, "master").text_origin("es", (None, "A"))
        assert (o.sha, o.verdict) == (ai, "ai")

    def test_msgid_edit_with_unchanged_text_is_looked_past(self, repo):
        human = self._commit(repo, 'msgid "Save  it"\nmsgstr "Guárdalo"', "Update Spanish", name="dcapillae")
        self._commit(repo, 'msgid "Save it"\nmsgstr "Guárdalo"', "Fix double space in source", name="Dev")
        o = port.OpenLibraryRepo(repo, "master").text_origin("es", (None, "Save it"))
        assert (o.sha, o.verdict) == (human, "human")
