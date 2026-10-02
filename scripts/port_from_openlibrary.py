#!/usr/bin/env python3
"""
Port openlibrary's translations into this repo, one class rule at a time.

Spec: https://github.com/internetarchive/openlibrary-i18n/issues/99

Every live entry (a msgid in this repo's messages.pot) is put in exactly one class by
comparing this repo's msgstr with openlibrary's at a pinned ref:

  only_here     openlibrary has nothing usable (missing, empty)      keep
  same          both non-fuzzy and identical                         keep
  ol_fuzzy      openlibrary's entry is fuzzy                         keep (never copied)
  plural_arity  openlibrary has a different number of plural forms   keep (issue #100)
  copy_empty    openlibrary non-fuzzy, empty here                    copy
  copy_fuzzy    openlibrary non-fuzzy, fuzzy here                    copy, non-fuzzy
  conflict      both non-fuzzy, different                            copy only if the
                                                                     openlibrary text is
                                                                     human-authored

Provenance of a conflict is the newest non-merge openlibrary commit that changed the
entry's text (lint markers included) or fuzzy state. A commit that left both unchanged (a
re-wrap, a template refactor, a msgid edit that kept the translation) is looked past.
Anything the rules cannot place is "indeterminate", and indeterminate never copies.

A copy is then excluded, and this repo's entry left untouched, if the copied entry would
fail tests/validators.py, tests/test_po_files.py::test_html_format, a %-placeholder check
over every plural form, would be altered by the daily run's `./i18n fix`, or is the
English msgid itself.

Retired strings (openlibrary translations on msgids its own messages.pot no longer has)
are never ported; they are only counted.

Usage:
  python scripts/port_from_openlibrary.py --openlibrary ~/Projects/openlibrary \\
      [--ref origin/master] [--lang es --lang ko] [--write [--sync-pot] [--az]] \\
      [--json report.json]

Without --write nothing under locale/ changes.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import importlib.util
import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from importlib.machinery import SourceFileLoader
from io import BytesIO
from pathlib import Path

from babel.messages.pofile import read_po, write_po

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIR = REPO_ROOT / "locale"
MESSAGES_POT = REPO_ROOT / "messages.pot"
OL_I18N = "openlibrary/i18n"

COPY_CLASSES = ("copy_empty", "copy_fuzzy")
CLASSES = ("only_here", "same", "ol_fuzzy", "plural_arity", "copy_empty", "copy_fuzzy", "conflict")

# Widths ./i18n and pybabel write with; a file keeps whichever one reproduces it.
WRAP_WIDTHS = (10000, 76)
# A commit that rewrote this many locales' .po files without being an AI run is a
# bulk sync (msgmerge, pot update); it did not author the text it touched.
BULK_LOCALES = 5


# ---------------------------------------------------------------------------
# The real toolbox and tests, loaded rather than reimplemented
# ---------------------------------------------------------------------------

def _load(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_modules: dict[str, object] = {}


def toolbox():
    """./i18n, the module the daily translate run executes."""
    if "i18n" not in _modules:
        _modules["i18n"] = _load("i18n_toolbox", REPO_ROOT / "i18n")
    return _modules["i18n"]


def validators():
    if "validators" not in _modules:
        _modules["validators"] = _load("i18n_validators", REPO_ROOT / "tests" / "validators.py")
    return _modules["validators"]


def html_test():
    """tests/test_po_files.py::test_html_format, the check validate-pr.yml runs."""
    if "html" not in _modules:
        _modules["html"] = _load("i18n_test_po_files", REPO_ROOT / "tests" / "test_po_files.py")
    return _modules["html"].test_html_format


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

def entry_key(msg) -> tuple:
    msgid = msg.id[0] if isinstance(msg.id, (tuple, list)) else msg.id
    return (msg.context, msgid)


def msg_forms(msg) -> tuple[str, ...]:
    if isinstance(msg.string, (tuple, list)):
        return tuple(s or "" for s in msg.string)
    return (msg.string or "",)


def is_translated(msg) -> bool:
    return any(msg_forms(msg))


def classify_entry(here, ol) -> str:
    """Class of a live entry. `here` is this repo's Message, `ol` openlibrary's or None."""
    if ol is None or not is_translated(ol):
        return "only_here"
    if ol.fuzzy:
        return "ol_fuzzy"
    if len(msg_forms(ol)) != len(msg_forms(here)):
        return "plural_arity"
    if not is_translated(here):
        return "copy_empty"
    if here.fuzzy:
        return "copy_fuzzy"
    if msg_forms(here) == msg_forms(ol):
        return "same"
    return "conflict"


def normalize_text(forms: tuple[str, ...]) -> tuple[str, ...]:
    """The translated text with whitespace differences removed. Lint markers are kept: they
    are part of what a port copies, so whoever changed one authored the difference."""
    return tuple(" ".join(f.split()) for f in forms)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

_PIPELINE_SUBJECT = re.compile(r"^i18n\([^)]+\):")
_AI_TEXT = re.compile(
    r"\bAI[- ](?:generated|translat)|claude|copilot|chatgpt|\bgpt-?\d|machine[- ]translat"
    r"|deepl|google translate",
    re.I,
)
_BOT_AUTHOR = re.compile(r"\[bot\]|github-actions|openlibrary-bot|claude|copilot", re.I)


def classify_commit(name: str, email: str, message: str, locales_touched: int = 1) -> tuple[str, str]:
    """('ai' | 'human' | 'indeterminate', reason) for the commit that wrote an entry's text."""
    subject = message.splitlines()[0] if message else ""
    if _BOT_AUTHOR.search(name) or _BOT_AUTHOR.search(email):
        return "ai", f"author {name}"
    if _PIPELINE_SUBJECT.match(subject):
        return "ai", "i18n(xx): pipeline subject"
    m = _AI_TEXT.search(message)
    if m:
        return "ai", f"message mentions {m.group(0)!r}"
    if locales_touched >= BULK_LOCALES:
        return "indeterminate", f"bulk commit touching {locales_touched} locales"
    return "human", f"author {name}"


def decide(cls: str, provenance: str | None) -> str:
    if cls in COPY_CLASSES:
        return "copy"
    if cls == "conflict" and provenance == "human":
        return "copy"
    return "keep"


@dataclass
class Origin:
    sha: str
    verdict: str
    reason: str
    looked_past: list[str] = field(default_factory=list)


class OpenLibraryRepo:
    """Read-only view of an openlibrary checkout at one ref. Never touches its worktree."""

    def __init__(self, path: Path, ref: str):
        self.path = Path(path)
        self.sha = self._git("rev-parse", f"{ref}^{{commit}}").strip()
        self._show: dict = {}
        self._index: dict = {}
        self._history: dict = {}
        self._commit: dict = {}

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.path, capture_output=True,
                              check=True, text=True).stdout

    def po_path(self, lang: str) -> str:
        return f"{OL_I18N}/{lang}/messages.po"

    def show(self, rev: str, path: str) -> bytes | None:
        if (rev, path) not in self._show:
            r = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=self.path, capture_output=True)
            self._show[rev, path] = r.stdout if r.returncode == 0 else None
        return self._show[rev, path]

    def catalog(self, rev: str, path: str) -> dict | None:
        """{key: Message} for the file at rev, or None where it does not exist."""
        if (rev, path) not in self._index:
            raw = self.show(rev, path)
            self._index[rev, path] = None if raw is None else {
                entry_key(m): m for m in read_po(BytesIO(raw)) if m.id}
        return self._index[rev, path]

    def history(self, path: str) -> list[str]:
        """Non-merge commits that changed path, newest first, reachable from the pinned ref.
        A merge is skipped so the walk reaches the branch commit that wrote the change, not
        whoever merged it (a merge's first-parent diff credits the merger with the branch)."""
        if path not in self._history:
            self._history[path] = self._git("log", "--no-merges", "--topo-order", "--format=%H",
                                            self.sha, "--", path).split()
        return self._history[path]

    def commit(self, sha: str) -> dict:
        if sha not in self._commit:
            out = self._git("show", "-s", "--format=%an%x00%ae%x00%P%x00%B", sha)
            name, email, parents, body = out.split("\x00", 3)
            files = self._git("show", "--format=", "--name-only", sha).split()
            touched = {f.split("/")[2] for f in files
                       if f.startswith(OL_I18N + "/") and f.endswith("/messages.po")}
            self._commit[sha] = dict(name=name, email=email,
                                     parent=(parents.split() or [None])[0],
                                     message=body.strip(), locales=len(touched))
        return self._commit[sha]

    def text_origin(self, lang: str, key: tuple) -> Origin:
        """The newest commit that changed this entry's text or fuzzy state, and its verdict.

        Walks the file's history rather than blaming its lines: blame cannot see a deleted
        line, and un-fuzzying an entry is exactly the deletion of its `#, fuzzy` line."""
        path, looked_past = self.po_path(lang), []
        head = (self.catalog(self.sha, path) or {}).get(key)
        if head is None:
            return Origin(self.sha, "indeterminate", "entry not found", looked_past)
        state = _state(head)
        for sha in self.history(path):
            msg = (self.catalog(sha, path) or {}).get(key)
            if msg is None or _state(msg) != state:
                continue  # a version later overwritten; not the one that survived
            c = self.commit(sha)
            prev = self.catalog(c["parent"], path) if c["parent"] else None
            same = _find_same(prev, key, msg, self.catalog(sha, path)) if prev is not None else None
            if same is None:
                verdict, reason = classify_commit(c["name"], c["email"], c["message"], c["locales"])
                return Origin(sha, verdict, reason, looked_past)
            looked_past.append(sha)
            key = entry_key(same)
        return Origin(self.sha, "indeterminate",
                      f"history ran out after looking past {len(looked_past)}", looked_past)


def _state(msg) -> tuple:
    return normalize_text(msg_forms(msg)), msg.fuzzy


def _find_same(parent: dict, key, msg, child: dict):
    """The parent's entry carrying this entry's text and fuzzy state, if one exists.

    Where the parent lacks the key, only a msgid the child no longer has can be this entry
    under its old name (a msgid edit). Another live msgid with the same translation is a
    different entry, and following it would credit that entry's author."""
    m = parent.get(key)
    if m is not None:
        return m if _state(m) == _state(msg) else None
    for k, m in parent.items():
        if k not in child and _state(m) == _state(msg):
            return m
    return None


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------

def _placeholders(s: str) -> set[str]:
    return {p for p in validators()._parse_cfmt(s) if p != "%%"}


def placeholder_mismatch(msg, num_plurals: int = 2) -> bool:
    """A form whose %-placeholders differ from its msgid's. Form 0 may follow either
    msgid where it is the n=1 form; where it is the only form (nplurals=1) it serves every
    n and must follow msgid_plural, as must every later form.
    Neither validators (Babel allows omissions) nor ./i18n fix (it compares against the
    singular) sees a later form dropping the count, the defect in issue #15."""
    forms = msg_forms(msg)
    if not isinstance(msg.id, (tuple, list)):
        return bool(forms[0]) and _placeholders(forms[0]) != _placeholders(msg.id)
    one, many = _placeholders(msg.id[0]), _placeholders(msg.id[1])
    if forms[0] and _placeholders(forms[0]) not in ((many,) if num_plurals == 1 else (one, many)):
        return True
    return any(f and _placeholders(f) != many for f in forms[1:])


FIX_STEPS = ("_fix_html_attrs", "_fix_format_type_mismatch", "_fix_format_errors",
             "_fix_validator_failures")


def fix_alters(catalog, keys) -> dict[tuple, str]:
    """Entries among `keys` that the daily run's ./i18n fix would change, and the first
    step that changes each. Runs the real steps on a copy of the whole catalog."""
    probe = copy.deepcopy(catalog)
    snap = lambda cat: {entry_key(m): (msg_forms(m), frozenset(m.flags)) for m in cat if m.id}
    altered, before = {}, snap(probe)
    for name in FIX_STEPS:
        getattr(toolbox(), name)(probe)
        after = snap(probe)
        for k in keys:
            if k not in altered and after.get(k) != before.get(k):
                altered[k] = name
        before = after
    return altered


# A lowercase word marks prose left in English; names, acronyms and domains (PDA, Open
# Library, Bookshop.org) are legitimately identical to their msgid. Some words are too
# ("total" in es). Excluding one keeps this repo's entry, which serves the same English or
# is refilled by the translate run, while copying English prose marks it done for good.
_ENGLISH_WORD = re.compile(r"(?<![\w%(.])[a-z]{2,}\b")


def exclusion_reasons(lang: str, msg, catalog) -> list[str]:
    """Why a copied entry must not ship, short of ./i18n fix (see fix_alters)."""
    reasons = [f"validators: {e.strip()}" for e in validators().validate(msg, catalog)]
    msgids = list(msg.id) if isinstance(msg.id, (tuple, list)) else [msg.id]
    pairs = [(i, f) for i, f in zip(msgids, msg_forms(msg)) if f]
    if pairs and all(i == f and _ENGLISH_WORD.search(i) for i, f in pairs):
        reasons.append("msgstr identical to msgid")
    if placeholder_mismatch(msg, catalog.num_plurals):
        reasons.append("placeholder mismatch")
    # Paired exactly as test_po_files.gen_po_msg_pairs pairs them.
    for msgid, form in zip(msgids, msg_forms(msg)):
        if form and "</" in msgid:
            try:
                html_test()(lang, msgid, form)
            except Exception as e:  # AssertionError, or ParseError on malformed markup
                reasons.append(f"test_html_format: {type(e).__name__}")
                break
    return reasons


# ---------------------------------------------------------------------------
# Porting one language
# ---------------------------------------------------------------------------

@dataclass
class Row:
    lang: str
    cls: str
    msgid: str
    context: str | None
    action: str                    # copy | keep | excluded
    here: tuple
    ol: tuple
    provenance: str | None = None
    origin: str | None = None
    reason: str | None = None
    excluded: list[str] = field(default_factory=list)


def apply_copy(here, ol) -> None:
    """Replace here's msgstr with openlibrary's, as a non-fuzzy entry."""
    here.string = tuple(ol.string) if isinstance(here.id, (tuple, list)) else ol.string
    here.flags.discard("fuzzy")
    here.previous_id = []


def port_catalog(lang: str, here_cat, ol_cat, live_keys: set, origin_of) -> list[Row]:
    """Mutates here_cat in place. origin_of(key) -> Origin, called only for conflicts."""
    ol_by_key = {entry_key(m): m for m in ol_cat if m.id}
    rows, originals = [], {}
    for here in here_cat:
        if not here.id or entry_key(here) not in live_keys:
            continue
        key = entry_key(here)
        ol = ol_by_key.get(key)
        cls = classify_entry(here, ol)
        row = Row(lang, cls, key[1], key[0], "keep", msg_forms(here),
                  msg_forms(ol) if ol is not None else ())
        if cls == "conflict":
            o = origin_of(key)
            row.provenance, row.origin, row.reason = o.verdict, o.sha[:10], o.reason
        if decide(cls, row.provenance) == "copy":
            originals[key] = copy.deepcopy(here)
            apply_copy(here, ol)
            row.excluded = exclusion_reasons(lang, here, here_cat)
            row.action = "copy"
        rows.append(row)
    copied = {(r.context, r.msgid) for r in rows if r.action == "copy" and not r.excluded}
    for key, step in fix_alters(here_cat, copied).items():
        next(r for r in rows if (r.context, r.msgid) == key).excluded.append(f"./i18n fix: {step}")
    for r in rows:
        if r.excluded:
            _restore(here_cat, originals[(r.context, r.msgid)])
            r.action = "excluded"
    return rows


def _restore(catalog, original) -> None:
    msg = catalog[original.id]
    msg.string, msg.flags, msg.previous_id = original.string, set(original.flags), original.previous_id


def retired(ol_cat, ol_live_keys: set) -> list[str]:
    """msgids openlibrary still translates though its own .pot no longer has them."""
    return [entry_key(m)[1] for m in ol_cat
            if m.id and is_translated(m) and entry_key(m) not in ol_live_keys]


def near_matches(msgids, live_msgids, cutoff: float = 0.8) -> dict[str, str]:
    live = list(live_msgids)
    out = {}
    for mid in set(msgids):
        hit = difflib.get_close_matches(mid, live, n=1, cutoff=cutoff)
        if hit:
            out[mid] = hit[0]
    return out


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def read_po_file(path: Path):
    raw = path.read_bytes()
    return raw, read_po(BytesIO(raw))


def wrap_width(raw: bytes, catalog) -> int:
    """The write width that reproduces this file, so a port does not re-wrap it."""
    for width in WRAP_WIDTHS:
        buf = BytesIO()
        write_po(buf, catalog, width=width, omit_header=False)
        if buf.getvalue() == raw:
            return width
    return WRAP_WIDTHS[0]


def write_po_file(path: Path, catalog, width: int) -> None:
    buf = BytesIO()
    write_po(buf, catalog, width=width, omit_header=False)
    path.write_bytes(buf.getvalue())


def port_az(repo: OpenLibraryRepo) -> dict:
    """Copy openlibrary's az catalog, update it against this .pot, compile it.
    Translations that cannot ship here are cleared; obsolete (retired) entries dropped."""
    tb = toolbox()
    (LOCALE_DIR / "az").mkdir(exist_ok=True)
    (LOCALE_DIR / "az" / "messages.po").write_bytes(repo.show(repo.sha, repo.po_path("az")))
    tb.sync("az")
    catalog = tb._read_po("az")
    translated = {entry_key(m) for m in catalog if m.id and is_translated(m)}
    bad = {entry_key(m): exclusion_reasons("az", m, catalog)
           for m in catalog if entry_key(m) in translated}
    for k, step in fix_alters(catalog, translated).items():
        bad[k].append(f"./i18n fix: {step}")
    cleared = {}
    for msg in catalog:
        if msg.id and bad.get(entry_key(msg)):
            cleared[entry_key(msg)[1]] = bad[entry_key(msg)]
            msg.string = ("",) * len(msg_forms(msg)) if isinstance(msg.id, (tuple, list)) else ""
    obsolete = sorted(entry_key(m)[1] for m in catalog.obsolete.values())
    catalog.obsolete.clear()
    tb._write_po("az", catalog)
    tb.compile_po("az")
    return dict(cleared=cleared, obsolete_dropped=obsolete,
                translated=sum(1 for m in catalog if m.id and is_translated(m)),
                total=sum(1 for m in catalog if m.id))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(args) -> dict:
    repo = OpenLibraryRepo(args.openlibrary, args.ref)
    if args.sync_pot:
        MESSAGES_POT.write_bytes(repo.show(repo.sha, f"{OL_I18N}/messages.pot"))
    here_pot = read_po(MESSAGES_POT.open("rb"))
    live = {entry_key(m) for m in here_pot if m.id}
    ol_pot = read_po(BytesIO(repo.show(repo.sha, f"{OL_I18N}/messages.pot")))
    ol_live = {entry_key(m) for m in ol_pot if m.id}
    here_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                              text=True).stdout.strip()
    langs = args.lang or sorted(p.name for p in LOCALE_DIR.iterdir()
                                if (p / "messages.po").exists())
    report = dict(openlibrary=repo.sha, here=here_sha, rows=[], retired={}, az=None)
    all_retired = []
    for lang in langs:
        path = LOCALE_DIR / lang / "messages.po"
        raw_ol = repo.show(repo.sha, repo.po_path(lang))
        if raw_ol is None:
            continue
        ol_cat = read_po(BytesIO(raw_ol))
        width = wrap_width(*read_po_file(path))
        if args.sync_pot:
            toolbox().sync(lang)
        _, here_cat = read_po_file(path)
        rows = port_catalog(lang, here_cat, ol_cat, live, lambda k, l=lang: repo.text_origin(l, k))
        report["rows"] += [r.__dict__ for r in rows]
        ret = retired(ol_cat, ol_live)
        report["retired"][lang] = len(ret)
        all_retired += ret
        if args.write and (args.sync_pot or any(r.action == "copy" for r in rows)):
            write_po_file(path, here_cat, width)
            toolbox().compile_po(lang)
    matches = near_matches(all_retired, {k[1] for k in live}, args.cutoff)
    report["retired_near"] = dict(
        unique_msgids=len(set(all_retired)), unique_near=len(matches),
        translations_near=sum(1 for m in all_retired if m in matches),
        sample=sorted(matches.items())[:15])
    if args.az and args.write:
        report["az"] = port_az(repo)
    return report


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--openlibrary", type=Path, required=True, help="path to an openlibrary clone")
    p.add_argument("--ref", default="origin/master")
    p.add_argument("--lang", action="append", help="limit to these languages (repeatable)")
    p.add_argument("--write", action="store_true", help="write .po/.mo files; default is a dry run")
    p.add_argument("--az", action="store_true", help="also create locale/az (requires --write)")
    p.add_argument("--sync-pot", action="store_true",
                   help="first copy openlibrary's messages.pot at --ref here and ./i18n sync each "
                        "language (requires --write)")
    p.add_argument("--cutoff", type=float, default=0.8, help="difflib ratio for retired near-matches")
    p.add_argument("--json", type=Path, help="write the full report as JSON")
    args = p.parse_args(argv)
    if (args.az or args.sync_pot) and not args.write:
        p.error("--az and --sync-pot change files; pass --write")
    report = run(args)
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    counts = Counter((r["lang"], r["cls"], r["action"]) for r in report["rows"])
    for (lang, cls, action), n in sorted(counts.items()):
        if cls not in ("only_here", "same"):
            print(f"{lang}\t{cls}\t{action}\t{n}")
    print(f"openlibrary {report['openlibrary']}  here {report['here']}")


if __name__ == "__main__":
    main()
