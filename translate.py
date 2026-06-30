#!/usr/bin/env python3
"""
Open Library i18n toolbox.

Subcommands (all operate on locale/<lang>/messages.po):
  sync <lang>                 update .po from messages.pot via pybabel
  status [lang ...]           translation coverage per language
  untranslated <lang>         JSON list of untranslated strings (stdout)
  apply <lang> <json_file>    write {msgid: msgstr} translations into .po
  fix <lang>                  fix HTML attrs and format-string errors
  validate <lang>             check format strings; exits non-zero on error
  test <lang>                 run pytest tests/test_po_files.py -k <lang>
  compile <lang>              compile .po -> .mo via pybabel
  download-pot                fetch messages.pot from openlibrary master
  plan                        show PR grouping plan (JSON); no side effects

All subcommands that touch files accept --dry-run.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).parent
LOCALE_DIR = REPO_ROOT / "locale"
MESSAGES_POT = REPO_ROOT / "messages.pot"
TESTS_DIR = REPO_ROOT / "tests"
BATCH_SIZE = 75

OL_MESSAGES_POT_URL = (
    "https://raw.githubusercontent.com/internetarchive/openlibrary"
    "/master/openlibrary/i18n/messages.pot"
)


def known_langs() -> list[str]:
    return sorted(
        p.name for p in LOCALE_DIR.iterdir()
        if p.is_dir() and (p / "messages.po").exists()
    )


# ---------------------------------------------------------------------------
# Catalog-level logic (pure functions — no filesystem, testable)
# ---------------------------------------------------------------------------

def _stats_from_catalog(catalog, lang: str) -> dict:
    total = fuzzy = translated = 0
    for msg in catalog:
        if not msg.id:
            continue
        total += 1
        if msg.fuzzy:
            fuzzy += 1
        elif isinstance(msg.string, str) and msg.string:
            translated += 1
        elif isinstance(msg.string, (list, tuple)) and any(msg.string):
            translated += 1
    untranslated = total - translated - fuzzy
    return {
        "lang": lang,
        "total": total,
        "translated": translated,
        "untranslated": untranslated,
        "fuzzy": fuzzy,
        "pct": round(100 * translated / total) if total else 0,
    }


def _untranslated_from_catalog(catalog, limit: int | None = None) -> list[dict]:
    entries = []
    for msg in catalog:
        if not msg.id or msg.fuzzy:
            continue
        if isinstance(msg.string, str) and msg.string:
            continue
        if isinstance(msg.string, (list, tuple)) and any(msg.string):
            continue
        entry = (
            {"id": msg.id[0], "id_plural": msg.id[1]}
            if isinstance(msg.id, tuple)
            else {"id": msg.id}
        )
        entries.append(entry)
        if limit and len(entries) >= limit:
            break
    return entries


def _apply_to_catalog(catalog, translations: dict) -> int:
    count = 0
    for msg in catalog:
        if not msg.id:
            continue
        key = msg.id[0] if isinstance(msg.id, tuple) else msg.id
        if key not in translations:
            continue
        val = translations[key]
        if isinstance(msg.id, tuple):
            msg.string = tuple(val) if isinstance(val, list) else (val, val)
        else:
            msg.string = val if isinstance(val, str) else str(val)
        count += 1
    return count


def _fix_html_attrs(catalog) -> int:
    """Add missing HTML attributes to msgstr by comparing with msgid. Returns count fixed."""
    def extract_attrs(tag_str: str) -> dict:
        return dict(re.findall(r'([\w-]+)=["\']([^"\']*)["\']', tag_str))

    def fix_str(msgid_str: str, msgstr_str: str) -> str:
        id_tags = re.findall(r'<([a-zA-Z][^>]*)>', msgid_str)
        str_tags = re.findall(r'<([a-zA-Z][^>]*)>', msgstr_str)
        if len(id_tags) != len(str_tags):
            return msgstr_str
        result = msgstr_str
        for id_tag, str_tag in zip(id_tags, str_tags):
            id_name = id_tag.split()[0] if id_tag.split() else ''
            str_name = str_tag.split()[0] if str_tag.split() else ''
            if id_name.lower() != str_name.lower():
                continue
            missing = {
                k: v for k, v in extract_attrs(id_tag).items()
                if k not in extract_attrs(str_tag)
            }
            if missing:
                new_tag = str_tag + ''.join(f' {k}="{v}"' for k, v in missing.items())
                result = result.replace(f'<{str_tag}>', f'<{new_tag}>', 1)
        return result

    fixed = 0
    for msg in catalog:
        if not msg.id or not msg.string:
            continue
        msgid = msg.id if isinstance(msg.id, str) else msg.id[0]
        if isinstance(msg.string, str):
            new = fix_str(msgid, msg.string)
            if new != msg.string:
                msg.string = new
                fixed += 1
        elif isinstance(msg.string, tuple):
            forms = tuple(fix_str(msgid, f) if f else f for f in msg.string)
            if forms != msg.string:
                msg.string = forms
                fixed += 1
    return fixed


def _fix_format_errors(catalog) -> int:
    """Clear msgstr for entries with placeholder mismatches. Returns count cleared."""
    def named(s: str) -> set:
        return set(re.findall(r'%\((\w+)\)[sdfr]', s))

    def positional(s: str) -> list:
        return re.findall(r'(?<!%)%[sdfr]', s)

    def mismatch(msgid, msgstr) -> bool:
        if not msgstr:
            return False
        id_s = msgid[0] if isinstance(msgid, tuple) else msgid
        ms_s = (msgstr[0] or '') if isinstance(msgstr, tuple) else msgstr
        id_n, ms_n = named(id_s), named(ms_s)
        if id_n - ms_n or ms_n - id_n:
            return True
        if len(positional(id_s)) != len(positional(ms_s)):
            return True
        if id_n and positional(ms_s) and not ms_n:
            return True
        if isinstance(msgid, tuple) and isinstance(msgstr, tuple):
            for form in msgstr:
                if form and (id_n - named(form)):
                    return True
        return False

    def bad_plural(msgid, msgstr) -> bool:
        if isinstance(msgid, tuple) and isinstance(msgstr, tuple):
            return (len(msgstr) >= 2 and bool(msgstr[0]) and not msgstr[1]) or not any(msgstr)
        return False

    cleared = 0
    for msg in catalog:
        if not msg.id or not msg.string:
            continue
        if mismatch(msg.id, msg.string) or bad_plural(msg.id, msg.string):
            msg.string = '' if not isinstance(msg.id, tuple) else ('', '')
            msg.flags.discard('fuzzy')
            cleared += 1
    return cleared


# ---------------------------------------------------------------------------
# File-level functions (lang name -> locale/<lang>/messages.po)
# ---------------------------------------------------------------------------

def _read_po(lang: str):
    from babel.messages.pofile import read_po
    with open(LOCALE_DIR / lang / "messages.po", "rb") as f:
        return read_po(f)


def _write_po(lang: str, catalog) -> None:
    from babel.messages.pofile import write_po
    with open(LOCALE_DIR / lang / "messages.po", "wb") as f:
        write_po(f, catalog, width=10000, omit_header=False)


def get_stats(lang: str) -> dict:
    return _stats_from_catalog(_read_po(lang), lang)


def get_untranslated(lang: str, limit: int | None = None) -> list[dict]:
    return _untranslated_from_catalog(_read_po(lang), limit)


def apply_translations(lang: str, translations: dict) -> int:
    catalog = _read_po(lang)
    count = _apply_to_catalog(catalog, translations)
    _write_po(lang, catalog)
    return count


def fix(lang: str) -> tuple[int, int]:
    """Returns (html_fixed, format_cleared)."""
    catalog = _read_po(lang)
    html = _fix_html_attrs(catalog)
    fmt = _fix_format_errors(catalog)
    _write_po(lang, catalog)
    return html, fmt


def validate(lang: str) -> list[tuple]:
    """Returns list of (msgid, errors). Empty list means valid."""
    sys.path.insert(0, str(TESTS_DIR))
    from validators import validate as _v
    catalog = _read_po(lang)
    errors = []
    for msg in catalog:
        if not msg.id:
            continue
        errs = _v(msg, catalog)
        if errs:
            errors.append((msg.id, errs))
    return errors


def _jobs_from_stats(stats: list[dict], batch_size: int = BATCH_SIZE) -> list[dict]:
    """
    Pure function: given a list of stats dicts, return a list of translation jobs.

    A job is {"langs": [lang, ...]}.  Each job maps to one PR.
    - Langs with 0 < untranslated <= batch_size are grouped into a single batch job.
    - Langs with untranslated > batch_size each get their own job.
    - Langs with untranslated == 0 are excluded.

    Within a job, Slater loops over each lang and, within each lang, over
    batches of batch_size strings — committing after each verified batch.
    """
    batch: list[str] = []
    individual: list[str] = []
    for s in stats:
        n = s["untranslated"]
        if n <= 0:
            continue
        if n <= batch_size:
            batch.append(s["lang"])
        else:
            individual.append(s["lang"])

    jobs: list[dict] = []
    if batch:
        jobs.append({"langs": batch})
    for lang in individual:
        jobs.append({"langs": [lang]})
    return jobs


def translation_jobs(langs: list[str] | None = None, batch_size: int = BATCH_SIZE) -> list[dict]:
    """File-level wrapper: read real stats, return job list."""
    stats = [get_stats(lang) for lang in (langs or known_langs())]
    return _jobs_from_stats(stats, batch_size)


# ---------------------------------------------------------------------------
# Subprocess commands
# ---------------------------------------------------------------------------

def download_pot(*, dry_run: bool = False) -> None:
    print("→ download messages.pot")
    if dry_run:
        return
    with urllib.request.urlopen(OL_MESSAGES_POT_URL) as resp:
        MESSAGES_POT.write_bytes(resp.read())


def sync(lang: str, *, dry_run: bool = False) -> None:
    print(f"→ pybabel update {lang}")
    if dry_run:
        return
    subprocess.run(
        [sys.executable, "-m", "babel.messages.frontend", "update",
         "--input-file", str(MESSAGES_POT),
         "--output-dir", str(LOCALE_DIR),
         "--locale", lang,
         "--no-fuzzy-matching"],
        check=True, cwd=REPO_ROOT,
    )


def compile_po(lang: str, *, dry_run: bool = False) -> None:
    print(f"→ pybabel compile {lang}")
    if dry_run:
        return
    subprocess.run(
        [sys.executable, "-m", "babel.messages.frontend", "compile",
         "--directory", str(LOCALE_DIR),
         "--locale", lang],
        check=True, cwd=REPO_ROOT,
    )


def run_tests(lang: str, *, dry_run: bool = False) -> None:
    print(f"→ pytest tests/test_po_files.py -k {lang}")
    if dry_run:
        return
    subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_po_files.py", "-k", lang, "-q"],
        check=True, cwd=REPO_ROOT,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _dry(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("--dry-run", action="store_true")
    return p


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open Library i18n toolbox",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    _dry(sub.add_parser("download-pot", help="Fetch messages.pot from openlibrary master"))

    p = _dry(sub.add_parser("sync", help="Update .po from messages.pot"))
    p.add_argument("lang")

    p = sub.add_parser("status", help="Translation coverage per language")
    p.add_argument("lang", nargs="*")

    p = sub.add_parser("untranslated", help="Print JSON list of untranslated strings")
    p.add_argument("lang")
    p.add_argument("--limit", type=int, default=None)

    p = sub.add_parser("apply", help="Apply {msgid: msgstr} JSON file to .po")
    p.add_argument("lang")
    p.add_argument("json_file")

    p = _dry(sub.add_parser("fix", help="Fix HTML attrs and format-string errors"))
    p.add_argument("lang")

    p = _dry(sub.add_parser("validate", help="Validate format strings"))
    p.add_argument("lang")

    p = _dry(sub.add_parser("test", help="Run pytest for a language"))
    p.add_argument("lang")

    p = _dry(sub.add_parser("compile", help="Compile .po -> .mo"))
    p.add_argument("lang")

    p = sub.add_parser("plan", help="Show PR grouping plan as JSON (no side effects)")
    p.add_argument("lang", nargs="*")

    args = parser.parse_args()

    if args.cmd == "download-pot":
        download_pot(dry_run=args.dry_run)

    elif args.cmd == "sync":
        sync(args.lang, dry_run=args.dry_run)

    elif args.cmd == "status":
        langs = args.lang or known_langs()
        rows = sorted([get_stats(la) for la in langs],
                      key=lambda r: r["untranslated"], reverse=True)
        hdr = f"{'lang':<6}  {'translated':>10}  {'untranslated':>12}  {'fuzzy':>5}  {'total':>6}  {'%':>3}"
        print(hdr)
        print("-" * len(hdr))
        for r in rows:
            flag = " !" if r["untranslated"] > 0 else ""
            print(f"{r['lang']:<6}  {r['translated']:>10}  {r['untranslated']:>12}  "
                  f"{r['fuzzy']:>5}  {r['total']:>6}  {r['pct']:>2}%{flag}")
        needs = sum(1 for r in rows if r["untranslated"] > 0)
        print(f"\n{needs}/{len(rows)} languages have untranslated strings")

    elif args.cmd == "untranslated":
        print(json.dumps(get_untranslated(args.lang, args.limit),
                         ensure_ascii=False, indent=2))

    elif args.cmd == "apply":
        translations = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        count = apply_translations(args.lang, translations)
        print(f"Applied: {count}")

    elif args.cmd == "fix":
        if args.dry_run:
            print(f"[dry-run] would fix {args.lang}")
            return
        html, fmt = fix(args.lang)
        print(f"{args.lang}: {html} HTML attr entries fixed, {fmt} format errors cleared")

    elif args.cmd == "validate":
        if args.dry_run:
            print(f"[dry-run] would validate {args.lang}")
            return
        errors = validate(args.lang)
        if errors:
            for msgid, errs in errors[:10]:
                print(f"error: {msgid!r}")
                for e in errs:
                    print(f"  {e}")
            sys.exit(f"{args.lang}: {len(errors)} error(s)")
        print(f"{args.lang}: OK")

    elif args.cmd == "test":
        run_tests(args.lang, dry_run=args.dry_run)

    elif args.cmd == "compile":
        compile_po(args.lang, dry_run=args.dry_run)

    elif args.cmd == "plan":
        langs = args.lang or None
        jobs = translation_jobs(langs)
        print(f"{len(jobs)} PR(s) to open:")
        for i, job in enumerate(jobs, 1):
            print(f"  PR {i}: langs={job['langs']}")
        print()
        print(json.dumps(jobs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
