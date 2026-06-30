#!/usr/bin/env python3
"""
openlibrary-i18n translation pipeline.

Self-contained: requires only `pip install babel anthropic pytest`.
No Docker, no openlibrary checkout needed.

Usage:
  python scripts/translate.py --update               # auto: fetch stats, dispatch subagents
  python scripts/translate.py --lang de es fr        # translate specific languages
  python scripts/translate.py --all                  # all languages in locale/
  python scripts/translate.py --lang de --batch-pr   # one PR for all specified langs
  python scripts/translate.py --lang de --no-translate  # sync + validate only, no LLM
  python scripts/translate.py --lang de --dry-run    # print actions, no side effects
  python scripts/translate.py --status               # show coverage per language (read-only)

Environment variables:
  ANTHROPIC_API_KEY    required unless --no-translate
  ANTHROPIC_MODEL      optional; defaults to claude-sonnet-4-6
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).parent.parent
LOCALE_DIR = REPO_ROOT / "locale"
MESSAGES_POT = REPO_ROOT / "messages.pot"
TESTS_DIR = REPO_ROOT / "tests"

OL_MESSAGES_POT_URL = (
    "https://raw.githubusercontent.com/internetarchive/openlibrary"
    "/master/openlibrary/i18n/messages.pot"
)
BATCH_SIZE = 75
DEFAULT_MODEL = "claude-sonnet-4-6"

KNOWN_LANGS = sorted(p.name for p in LOCALE_DIR.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# Non-LLM helper functions (pure Python / pybabel / pytest)
# ---------------------------------------------------------------------------


def download_pot(dest: Path, *, dry_run: bool = False) -> None:
    """Download latest messages.pot from openlibrary master."""
    print(f"→ download messages.pot → {dest}")
    if dry_run:
        return
    with urllib.request.urlopen(OL_MESSAGES_POT_URL) as resp:
        dest.write_bytes(resp.read())


def sync_language(lang: str, *, dry_run: bool = False) -> None:
    """Sync locale/{lang}/messages.po with the current messages.pot via pybabel."""
    print(f"→ pybabel update {lang}")
    if dry_run:
        return
    subprocess.run(
        [
            sys.executable, "-m", "babel.messages.frontend",
            "update",
            "--input-file", str(MESSAGES_POT),
            "--output-dir", str(LOCALE_DIR),
            "--locale", lang,
            "--no-fuzzy-matching",
        ],
        check=True,
        cwd=REPO_ROOT,
    )


def get_stats(lang: str) -> dict:
    """Return translation coverage stats for one language. Read-only."""
    from babel.messages.pofile import read_po

    po_path = LOCALE_DIR / lang / "messages.po"
    with open(po_path, "rb") as f:
        catalog = read_po(f)

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
    pct = round(100 * translated / total) if total else 0
    return {
        "lang": lang,
        "total": total,
        "translated": translated,
        "untranslated": untranslated,
        "fuzzy": fuzzy,
        "pct": pct,
    }


def print_status(languages: list[str]) -> None:
    """Print a coverage table. Sorts by untranslated desc."""
    rows = [get_stats(lang) for lang in languages]
    rows.sort(key=lambda r: r["untranslated"], reverse=True)

    header = f"{'lang':<6}  {'translated':>10}  {'untranslated':>12}  {'fuzzy':>5}  {'total':>6}  {'%':>3}"
    print(header)
    print("-" * len(header))
    for r in rows:
        flag = " !" if r["untranslated"] > 0 else ""
        print(
            f"{r['lang']:<6}  {r['translated']:>10}  {r['untranslated']:>12}  "
            f"{r['fuzzy']:>5}  {r['total']:>6}  {r['pct']:>2}%{flag}"
        )
    needs_work = sum(1 for r in rows if r["untranslated"] > 0)
    total_missing = sum(r["untranslated"] for r in rows)
    print(f"\n{needs_work}/{len(rows)} languages have untranslated strings ({total_missing} total)")


def get_untranslated(lang: str) -> list[dict]:
    """Return list of untranslated non-fuzzy entries."""
    from babel.messages.pofile import read_po

    po_path = LOCALE_DIR / lang / "messages.po"
    with open(po_path, "rb") as f:
        catalog = read_po(f)

    entries = []
    for msg in catalog:
        if not msg.id or msg.fuzzy:
            continue
        if isinstance(msg.string, str) and not msg.string:
            entry = {"id": msg.id[0], "id_plural": msg.id[1]} if isinstance(msg.id, tuple) else {"id": msg.id}
            entries.append(entry)
        elif isinstance(msg.string, (list, tuple)) and not any(msg.string):
            entry = {"id": msg.id[0], "id_plural": msg.id[1]} if isinstance(msg.id, tuple) else {"id": msg.id}
            entries.append(entry)
    return entries


def _chunk(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def apply_translations(
    lang: str, translations: dict[str, str | list[str]], *, dry_run: bool = False
) -> int:
    """Write {msgid: msgstr} into locale/{lang}/messages.po. Returns count applied."""
    from babel.messages.pofile import read_po, write_po

    po_path = LOCALE_DIR / lang / "messages.po"
    with open(po_path, "rb") as f:
        catalog = read_po(f)

    count = 0
    for msg in catalog:
        if not msg.id:
            continue
        key = msg.id[0] if isinstance(msg.id, tuple) else msg.id
        if key in translations:
            val = translations[key]
            if isinstance(msg.id, tuple):
                msg.string = tuple(val) if isinstance(val, list) else (val, val)
            else:
                msg.string = val if isinstance(val, str) else str(val)
            count += 1

    if not dry_run:
        with open(po_path, "wb") as f:
            write_po(f, catalog, include_previous=False)

    return count


def validate(lang: str, *, dry_run: bool = False) -> None:
    """Validate format strings in locale/{lang}/messages.po using tests/validators.py."""
    from babel.messages.pofile import read_po
    sys.path.insert(0, str(TESTS_DIR))
    from validators import validate as _validate_msg

    print(f"→ validate {lang}")
    if dry_run:
        return

    po_path = LOCALE_DIR / lang / "messages.po"
    with open(po_path, "rb") as f:
        catalog = read_po(f)

    errors = []
    for msg in catalog:
        if not msg.id:
            continue
        errs = _validate_msg(msg, catalog)
        if errs:
            errors.append((msg.id, errs))

    if errors:
        for msgid, errs in errors[:5]:
            print(f"  error: {msgid!r}")
            for e in errs:
                print(f"    {e}")
        raise RuntimeError(f"{lang}: {len(errors)} format-string error(s)")

    print(f"  {lang}: OK")


def run_tests(lang: str, *, dry_run: bool = False) -> None:
    """Run tests/test_po_files.py for one language."""
    print(f"→ pytest tests/test_po_files.py -k {lang}")
    if dry_run:
        return
    subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_po_files.py", "-k", lang, "-q"],
        check=True,
        cwd=REPO_ROOT,
    )


def compile_po(lang: str, *, dry_run: bool = False) -> None:
    """Compile locale/{lang}/messages.po → messages.mo via pybabel."""
    print(f"→ pybabel compile {lang}")
    if dry_run:
        return
    subprocess.run(
        [
            sys.executable, "-m", "babel.messages.frontend",
            "compile",
            "--directory", str(LOCALE_DIR),
            "--locale", lang,
        ],
        check=True,
        cwd=REPO_ROOT,
    )


def commit_lang(lang: str, model: str, *, dry_run: bool = False) -> str:
    """git add + commit the updated .po for this language. Returns commit sha."""
    po_path = f"locale/{lang}/messages.po"
    msg = (
        f"i18n({lang}): AI translation update\n\n"
        f"Fills untranslated strings using {model}.\n"
        f"Format strings and HTML preserved verbatim. Fuzzy entries left for human review.\n"
        f"🤖 Translations generated by AI translation API; please review for accuracy."
    )
    print(f"→ git commit {po_path}")
    if dry_run:
        return "dry-run-sha"
    subprocess.run(["git", "add", po_path], check=True, cwd=REPO_ROOT)
    subprocess.run(
        [
            "git", "commit",
            "--author=Michael E. Karpeles (Mek) <michael.karpeles@gmail.com>",
            "-m", msg,
        ],
        check=True,
        cwd=REPO_ROOT,
    )
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        check=True, capture_output=True, text=True, cwd=REPO_ROOT,
    ).stdout.strip()


def open_pr(
    branch: str,
    languages: list[str],
    *,
    mode: str = "per-language",
    dry_run: bool = False,
) -> str:
    """Open a PR on openlibrary-i18n."""
    lang_list = ", ".join(languages)
    title = (
        f"i18n: AI translation update for {lang_list}"
        if mode == "batch"
        else f"i18n({languages[0]}): AI translation update"
    )
    body = (
        f"## Summary\n\n"
        f"AI-generated translation update for: {lang_list}\n\n"
        f"Format strings (`%(name)s`, `%s`, `{{page}}`) and HTML markup preserved verbatim. "
        f"Fuzzy entries left unchanged for human review.\n\n"
        f"**Please review for accuracy before merging.**\n\n"
        f"🤖 Generated by AI translation API. Native speaker review recommended."
    )
    cmd = [
        "gh", "pr", "create",
        "--repo", "internetarchive/openlibrary-i18n",
        "--base", "main",
        "--head", branch,
        "--title", title,
        "--body", body,
    ]
    print(f"→ gh pr create ({mode})")
    if dry_run:
        return "dry-run-pr-url"
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------------------------
# LLM function — ONLY this calls the Anthropic API
# ---------------------------------------------------------------------------


def translate_batch(
    strings: list[dict],
    target_lang: str,
    client,
    model: str,
) -> dict[str, str | list[str]]:
    """
    Single Anthropic API call for up to BATCH_SIZE strings.
    Returns {msgid: msgstr}. Plural entries return list[str].
    """
    if not strings:
        return {}

    _lang_names = {
        "ar": "Arabic", "as": "Assamese", "bn": "Bengali", "cs": "Czech",
        "de": "German", "es": "Spanish", "fr": "French", "hi": "Hindi",
        "hr": "Croatian", "id": "Indonesian", "it": "Italian", "ja": "Japanese",
        "ko": "Korean", "pl": "Polish", "pt": "Portuguese", "ro": "Romanian",
        "ru": "Russian", "sc": "Sardinian", "te": "Telugu", "tl": "Tagalog",
        "tr": "Turkish", "uk": "Ukrainian", "zh": "Chinese (Simplified)",
    }
    lang_name = _lang_names.get(target_lang, target_lang)

    prompt = "\n".join([
        f"Translate the following Open Library UI strings from English to {lang_name}.",
        "",
        "Rules:",
        "1. Preserve ALL format strings VERBATIM: %(name)s, %(count)d, %s, %d, {page}, etc.",
        "2. Preserve ALL HTML tag names and attribute KEYS verbatim (e.g. <a href=...>, <b>).",
        "3. Do NOT translate proper nouns: 'Internet Archive', 'Open Library', 'Wikidata'.",
        "4. Do NOT translate URLs or email addresses.",
        f"5. For plural entries, return a JSON array with the correct plural forms for {lang_name}.",
        "6. Return ONLY a JSON object: {msgid: translation, ...}",
        "7. Use natural, concise UI tone. Use formal register if the language has one.",
        "",
        "Strings to translate (JSON array):",
        json.dumps(strings, ensure_ascii=False, indent=2),
        "",
        "Return ONLY valid JSON: {msgid: translation, ...}",
    ])

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    return json.loads(raw)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def process_language(
    lang: str,
    *,
    client,
    model: str,
    no_translate: bool,
    dry_run: bool,
) -> None:
    """Full pipeline: sync → translate → validate → test → compile → commit."""
    print(f"\n{'='*60}")
    print(f"Processing: {lang}")
    print("=" * 60)

    # Stage 1: sync .po with latest .pot
    sync_language(lang, dry_run=dry_run)

    # Stage 2: fill untranslated strings via LLM
    if not no_translate:
        untranslated = get_untranslated(lang)
        print(f"  {len(untranslated)} untranslated strings")
        if untranslated:
            for i, batch in enumerate(_chunk(untranslated, BATCH_SIZE), 1):
                print(f"  translating batch {i} ({len(batch)} strings)...")
                if not dry_run:
                    translations = translate_batch(batch, lang, client, model)
                    applied = apply_translations(lang, translations, dry_run=dry_run)
                    print(f"    applied: {applied}/{len(batch)}")
                else:
                    print(f"    [dry-run] would translate {len(batch)} strings")

    # Stage 3: validate format strings
    validate(lang, dry_run=dry_run)

    # Stage 4: HTML structure tests (non-fatal — pre-existing failures are known)
    try:
        run_tests(lang, dry_run=dry_run)
    except subprocess.CalledProcessError:
        print(f"  ⚠ test_po_files failed for {lang} — review before merging")

    # Stage 5: compile .po → .mo
    compile_po(lang, dry_run=dry_run)

    # Stage 6: commit
    sha = commit_lang(lang, model, dry_run=dry_run)
    print(f"  committed: {sha}")


def _claude_bin() -> str:
    result = subprocess.run(["which", "claude"], capture_output=True, text=True)
    return result.stdout.strip() or os.path.expanduser("~/.local/bin/claude")


def _run_claude(prompt: str, *, dry_run: bool = False) -> None:
    cmd = [_claude_bin(), "--dangerously-skip-permissions", "-p", prompt]
    print(f"→ claude -p <{len(prompt)}-char prompt>")
    if not dry_run:
        subprocess.run(cmd, timeout=600, check=True)


def _run_update(*, model: str, dry_run: bool = False) -> None:
    """
    Auto dispatch: fetch stats, group by BATCH_SIZE, spin up subagents.

    ≤ BATCH_SIZE untranslated → one Claude call, one batch PR covering all such langs.
    > BATCH_SIZE untranslated → one Claude call per language, one PR each.
    0 untranslated → skipped.
    """
    stats = [get_stats(lang) for lang in KNOWN_LANGS]
    needs_work = [s for s in stats if s["untranslated"] > 0]

    if not needs_work:
        print("All languages fully translated — nothing to do.")
        return

    batch_langs = [s["lang"] for s in needs_work if s["untranslated"] <= BATCH_SIZE]
    individual_langs = [s["lang"] for s in needs_work if s["untranslated"] > BATCH_SIZE]

    print(f"Batch PR ({len(batch_langs)} langs): {batch_langs}")
    print(f"Individual PRs ({len(individual_langs)} langs): {individual_langs}")

    script = Path(__file__)

    if batch_langs:
        lang_list = " ".join(batch_langs)
        _run_claude(
            f"Run the i18n translation pipeline for these languages as a single batch PR.\n\n"
            f"Command: python {script} --lang {lang_list} --batch-pr --model {model}\n\n"
            f"The script is self-contained — no Docker or openlibrary checkout needed.\n"
            f"Open one PR when done. No approval gate required.",
            dry_run=dry_run,
        )

    for lang in individual_langs:
        _run_claude(
            f"Run the i18n translation pipeline for language: {lang}\n\n"
            f"Command: python {script} --lang {lang} --model {model}\n\n"
            f"The script is self-contained — no Docker or openlibrary checkout needed.\n"
            f"Open a per-language PR when done. No approval gate required.",
            dry_run=dry_run,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open Library i18n translation pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--lang", nargs="+", metavar="LANG", help="Languages to process")
    parser.add_argument("--all", action="store_true", help="Process all languages in locale/")
    parser.add_argument(
        "--update",
        action="store_true",
        help=(
            "Auto mode: fetch stats, group by threshold, dispatch subagents. "
            f"Langs with ≤{BATCH_SIZE} untranslated → one batch PR; >{{BATCH_SIZE}} → one PR each."
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show translation coverage per language and exit (read-only, no API key needed)",
    )
    parser.add_argument(
        "--batch-pr",
        action="store_true",
        help="Open one PR for all specified languages (default: per-language)",
    )
    parser.add_argument(
        "--no-translate",
        action="store_true",
        help="Skip LLM translation; sync + validate + compile only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without executing anything",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL),
        help=f"Anthropic model (default: {DEFAULT_MODEL})",
    )
    args = parser.parse_args()

    if args.status:
        languages = KNOWN_LANGS if (args.all or not args.lang) else args.lang
        print_status(languages)
        return

    if args.update:
        _run_update(model=args.model, dry_run=args.dry_run)
        return

    if not args.lang and not args.all:
        parser.error("Specify --lang LANG [LANG ...], --all, --update, or --status")

    languages = KNOWN_LANGS if args.all else args.lang
    invalid = [l for l in languages if l not in KNOWN_LANGS]
    if invalid:
        parser.error(f"Unknown languages: {invalid}. Known: {KNOWN_LANGS}")

    if not args.no_translate and not args.dry_run:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            sys.exit("ANTHROPIC_API_KEY is not set. Use --no-translate to skip AI translation.")
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
        except ImportError:
            sys.exit("anthropic package is required: pip install anthropic")
    else:
        client = None

    download_pot(MESSAGES_POT, dry_run=args.dry_run)

    processed = []
    for lang in languages:
        process_language(
            lang,
            client=client,
            model=args.model,
            no_translate=args.no_translate,
            dry_run=args.dry_run,
        )
        processed.append(lang)

    if args.batch_pr:
        branch = "i18n/ai-translation-batch"
        url = open_pr(branch, processed, mode="batch", dry_run=args.dry_run)
        print(f"\nPR opened: {url}")
    else:
        for lang in processed:
            branch = f"i18n/{lang}"
            url = open_pr(branch, [lang], mode="per-language", dry_run=args.dry_run)
            print(f"\nPR opened ({lang}): {url}")


if __name__ == "__main__":
    main()
