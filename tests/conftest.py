"""Shared pytest options for the i18n test suite.

``--locale`` runs only the tests for the given languages. It filters on each
test's ``locale`` parameter, never on the test's name: ``-k tr`` used to match
every ``<strong>`` in a msgid, and ``-k es`` matched ``test_po_files`` itself,
so "test one language" quietly ran nearly every language (#302).
"""
from __future__ import annotations

from pathlib import Path

import pytest

LOCALE_DIR = Path(__file__).resolve().parent.parent / "locale"


def pytest_addoption(parser):
    parser.addoption(
        "--locale",
        action="append",
        default=[],
        metavar="LANG",
        help="Only run tests for these locales. Repeat it, or pass several "
             "space- or comma-separated codes in one value.",
    )


def requested_locales(values: list[str]) -> set[str]:
    """Split --locale values ("ca", "ca sq", "ca,sq") into a set of codes."""
    return {code for v in values for code in v.replace(",", " ").split()}


def pytest_configure(config):
    wanted = requested_locales(config.getoption("locale"))
    unknown = sorted(c for c in wanted if not (LOCALE_DIR / c / "messages.po").exists())
    if unknown:
        raise pytest.UsageError(f"--locale: no locale/<lang>/messages.po for {', '.join(unknown)}")


def pytest_collection_modifyitems(config, items):
    wanted = requested_locales(config.getoption("locale"))
    if not wanted:
        return
    keep, drop = [], []
    for item in items:
        params = getattr(getattr(item, "callspec", None), "params", {})
        # Tests that aren't per-locale (no `locale` parameter) always run.
        if "locale" not in params or params["locale"] in wanted:
            keep.append(item)
        else:
            drop.append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
