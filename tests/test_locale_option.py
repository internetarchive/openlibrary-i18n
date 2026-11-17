"""Tests for the --locale pytest option (tests/conftest.py).

`./i18n test {lang}` and validate-pr.yml use it to run only some languages'
tests. It replaced `-k {lang}`, a substring match on the test id that also hit
msgid text: "tr" matched every <strong>, "es" matched test_po_files itself (#302).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import requested_locales

TESTS_DIR = Path(__file__).resolve().parent
PO_TESTS = TESTS_DIR / "test_po_files.py"


def _collect(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", str(PO_TESTS), *args],
        capture_output=True, text=True, cwd=TESTS_DIR.parent,
    )


def _locales_of(stdout: str) -> set[str]:
    """Locales of collected test_html_format ids, e.g. `test_html_format[tr-...]` -> tr."""
    return set(re.findall(r"test_html_format\[([a-z]{2,3})-", stdout))


@pytest.mark.parametrize(("values", "expected"), [
    ([], set()),
    (["ca"], {"ca"}),
    (["ca sq"], {"ca", "sq"}),
    (["ca,sq", "tr"], {"ca", "sq", "tr"}),
    (["  ca  ", "ca"], {"ca"}),
])
def test_requested_locales_splits_and_dedupes(values, expected):
    assert requested_locales(values) == expected


@pytest.mark.parametrize("lang", ["tr", "es"])
def test_selects_only_that_locale(lang):
    """tr and es are substrings of shared text, so -k selected every locale for them."""
    r = _collect("--locale", lang)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _locales_of(r.stdout) == {lang}


def test_several_locales_in_one_value():
    r = _collect("--locale", "ca sq")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _locales_of(r.stdout) == {"ca", "sq"}


def test_unknown_locale_is_a_usage_error():
    r = _collect("--locale", "xx")
    assert r.returncode == 4
    assert "--locale: no locale/<lang>/messages.po for xx" in r.stderr
