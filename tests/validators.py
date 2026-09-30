"""
Format-string validation for Open Library .po files.

Adapted from openlibrary/openlibrary/i18n/validators.py.
"""
from __future__ import annotations

import gettext
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from babel.messages.catalog import Catalog, Message


def validate(message: Message, catalog: Catalog) -> list[str]:
    errors = [f"    {err}" for err in message.check(catalog)]
    if (
        errors
        and message.pluralizable
        and _form0_selected_beyond_one(catalog)
        and _form0_may_carry_plural_placeholders(message)
    ):
        # msgstr[0] is also shown for some n other than 1 (every n with
        # nplurals=1, n=21, 31, ... in ru/uk/hr), so it may instead answer to
        # msgid_plural, the English it stands in for there (#15).
        from babel.messages.catalog import Message

        as_plural = Message(
            (message.id[1], message.id[1]),
            message.string,
            flags=message.flags,
            context=message.context,
        )
        if not as_plural.check(catalog):
            errors = []
    if message.python_format and not message.pluralizable and message.string:
        errors.extend(_validate_cfmt(str(message.id or ""), str(message.string or "")))
    return errors


def _form0_selected_beyond_one(catalog: Catalog) -> bool:
    try:
        rule = gettext.c2py(catalog.plural_expr)
        return any(rule(n) == 0 for n in range(1000) if n != 1)
    except (ValueError, ArithmeticError):
        # A rule gettext cannot evaluate: keep the singular-only check.
        return False


_NAMED = re.compile(r"%\((\w+)\)")


def _form0_may_carry_plural_placeholders(message: Message) -> bool:
    """
    The same shape `./i18n fix` keeps: msgstr[0] holds every placeholder of the
    singular, nothing outside the two msgids, and msgid_plural brings something
    the singular lacks. Without that last clause a placeholder-free msgid_plural
    would turn Babel's check off entirely.
    """
    singular, plural = (str(m) for m in message.id)
    form0 = str(message.string[0] if message.string else "")

    def positional(s: str) -> int:
        return sum(1 for p in _parse_cfmt(s) if p != "%%" and not p.startswith("%("))

    sing_n, plur_n, form_n = (set(_NAMED.findall(s)) for s in (singular, plural, form0))
    mixed_kinds = (positional(singular) and plur_n) or (sing_n and positional(plural))
    brings_more = bool(plur_n - sing_n) or positional(plural) > positional(singular)
    # The last two clauses are also enforced by Babel's check against msgid_plural
    # below; they are kept so this rule does not rest on Babel's internals, which is
    # how a placeholder-free msgid_plural once switched the check off.
    return (
        not mixed_kinds
        and brings_more
        and sing_n <= form_n
        and form_n <= sing_n | plur_n
        and positional(form0) in {positional(singular), positional(plural)}
    )


def _validate_cfmt(msgid: str, msgstr: str) -> list[str]:
    errors = []
    if _cfmt_fingerprint(msgid) != _cfmt_fingerprint(msgstr):
        errors.append("    Failed custom string format validation")
    return errors


def _cfmt_fingerprint(string: str) -> tuple[tuple[str, ...], frozenset[str]]:
    """
    Get a fingerprint of the C-style format specifiers in a string: positional
    placeholders in order, since `%` consumes them in sequence, and the set of
    everything else, since named placeholders are looked up by key and may be
    reordered or repeated. Mirrors openlibrary#13780.

    >>> _cfmt_fingerprint('hello %s and %d')
    (('%s', '%d'), frozenset())
    >>> _cfmt_fingerprint('hello %s and %d') == _cfmt_fingerprint('%d and %s')
    False
    >>> _cfmt_fingerprint('%(username)s read %(total)d. Join %(username)s') == _cfmt_fingerprint('%(total)d read by %(username)s')
    True
    """
    pieces = _parse_cfmt(string)
    positional = tuple(p for p in pieces if p != "%%" and not p.startswith("%("))
    return positional, frozenset(p for p in pieces if p == "%%" or p.startswith("%("))


def _parse_cfmt(string: str):
    """
    Extract C-style Python format specifiers from a string.

    >>> _parse_cfmt('hello %s')
    ['%s']
    >>> _parse_cfmt(' by %(name)s')
    ['%(name)s']
    >>> _parse_cfmt('%(count)d Lists')
    ['%(count)d']
    >>> _parse_cfmt('100%% Complete!')
    ['%%']
    >>> _parse_cfmt('%(name)s avez %(count)s listes.')
    ['%(name)s', '%(count)s']
    >>> _parse_cfmt('')
    []
    >>> _parse_cfmt('Hello World')
    []
    """
    cfmt_re = r"""
        (
            %(?:
                (?:\([a-zA-Z_][a-zA-Z0-9_]*?\))?   # e.g. %(blah)s
                (?:[-+0 #]{0,5})                   # optional flags
                (?:\d+|\*)?                        # width
                (?:\.(?:\d+|\*))?                  # precision
                (?:h|l|ll|w|I|I32|I64)?            # size
                [cCdiouxXeEfgGaAnpsSZ]             # type
            )
        )
        |                                # OR
        %%                               # literal "%%"
    """
    return [m.group(0) for m in re.finditer(cfmt_re, string, flags=re.VERBOSE)]
