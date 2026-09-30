"""
Plural-entry and header checks over the real locale/ catalogs.

Each test covers one defect class, per locale, on non-fuzzy entries only
(fuzzy entries never reach a compiled .mo):

- An empty plural form that the language's plural rule can select. Babel's
  write_mo substitutes the English msgid for an empty form, so that n renders
  English (#100).
- A plural form that drops a placeholder of the English it stands in for, e.g.
  the count (#15). A form the plural rule selects for any n other than 1 stands
  in for msgid_plural, so a hardcoded "1" there is wrong at n=21 (ru) or n=5
  (nplurals=1).
- A header whose Language: disagrees with its directory (#90).
"""
from __future__ import annotations

import gettext
import re
from pathlib import Path

import pytest
from babel.messages.pofile import read_po

LOCALE_DIR = Path(__file__).parent.parent / "locale"

# Same %-specifier grammar the toolbox's format checks use.
_PLACEHOLDER = re.compile(r"%\((\w+)\)[sdifruoxXeEgGc]|(?<!%)(%[sdifruoxXeEgGc])")

# Locales that still carry empty reachable plural forms, left for a follow-up.
# strict: filling one turns its xfail into a failure until it is removed here.
KNOWN_EMPTY_FORMS = {"cs", "hr", "ro", "ru", "uk"}

# Locales whose form 0 still hardcodes "1"/"one" while also covering other n,
# left for the same follow-up. The entries that would regress against
# openlibrary are fixed; these are the rest. strict, as above.
KNOWN_DROPPED_PLACEHOLDERS = {"ar", "fr", "hr", "uk"}


def _locales() -> list[str]:
    return sorted(p.name for p in LOCALE_DIR.iterdir() if (p / "messages.po").exists())


def _catalog(locale: str):
    with open(LOCALE_DIR / locale / "messages.po", "rb") as f:
        return read_po(f)


def _placeholders(s: str) -> set[str]:
    return {named or positional for named, positional in _PLACEHOLDER.findall(s)}


def _numbers_by_form(catalog) -> dict[int, list[int]]:
    """Which n in 0..999 select each plural form, per the catalog's own rule."""
    rule = gettext.c2py(catalog.plural_expr)
    forms: dict[int, list[int]] = {i: [] for i in range(catalog.num_plurals)}
    for n in range(1000):
        forms.setdefault(rule(n), []).append(n)
    return forms


def _translated_plurals(catalog):
    for msg in catalog:
        if msg.id and msg.pluralizable and not msg.fuzzy and any(msg.string):
            yield msg


def _mark_known(locale: str, known: set[str]):
    if locale in known:
        return pytest.param(locale, marks=pytest.mark.xfail(strict=True, reason="follow-up"))
    return locale


@pytest.mark.parametrize("locale", [_mark_known(loc, KNOWN_EMPTY_FORMS) for loc in _locales()])
def test_no_reachable_plural_form_is_empty(locale: str):
    catalog = _catalog(locale)
    reachable = [i for i, ns in _numbers_by_form(catalog).items() if ns]
    offenders = []
    for msg in _translated_plurals(catalog):
        forms = list(msg.string) + [""] * (catalog.num_plurals - len(msg.string))
        empty = [i for i in reachable if not forms[i]]
        if empty:
            offenders.append((msg.id[0], empty))
    assert offenders == []


@pytest.mark.parametrize("locale", [_mark_known(loc, KNOWN_DROPPED_PLACEHOLDERS) for loc in _locales()])
def test_plural_forms_keep_placeholders_of_their_english(locale: str):
    """A form selected only by n=1 answers to the singular msgid; any other form
    to msgid_plural.

    With nplurals=1 the single form is shown for every n: "1人" hardcoded there
    reads "1 person" for 50 people. In ru, form 0 is also shown at n=21, 31, ...
    """
    catalog = _catalog(locale)
    beyond_one = {i for i, ns in _numbers_by_form(catalog).items() if set(ns) - {1}}
    offenders = []
    for msg in _translated_plurals(catalog):
        singular, plural = msg.id
        for i, form in enumerate(msg.string):
            if not form:
                continue
            english = plural if i in beyond_one else singular
            missing = _placeholders(english) - _placeholders(form)
            if missing:
                offenders.append((singular, i, sorted(missing)))
    assert offenders == []


@pytest.mark.parametrize("locale", _locales())
def test_header_language_matches_directory(locale: str):
    assert str(_catalog(locale).locale_identifier) == locale
