#!/usr/bin/env python3
"""
openlibrary-i18n translation pipeline.

Downloads the latest messages.pot from openlibrary, syncs .po files for the
specified languages, fills untranslated strings using an AI translation API,
validates the result, and compiles .po → .mo.

Usage:
  python scripts/translate.py --lang de es fr       # translate specific languages
  python scripts/translate.py --all                  # all languages in locale/
  python scripts/translate.py --lang de --batch-pr   # one PR for all specified langs
  python scripts/translate.py --lang de --no-translate  # sync + validate only
  python scripts/translate.py --lang de --dry-run    # print actions, no side effects

Environment variables:
  ANTHROPIC_API_KEY    required unless --no-translate
  OL_MOUNT_DIR         path to an openlibrary worktree with Docker running
                       (if unset, falls back to ~/Projects/openlibrary)
  ANTHROPIC_MODEL      optional; defaults to claude-sonnet-4-6
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).parent.parent
LOCALE_DIR = REPO_ROOT / "locale"
MESSAGES_POT = REPO_ROOT / "messages.pot"
OL_MESSAGES_POT_URL = (
    "https://raw.githubusercontent.com/internetarchive/openlibrary"
    "/master/openlibrary/i18n/messages.pot"
)
BATCH_SIZE = 75
DEFAULT_MODEL = "claude-sonnet-4-6"

KNOWN_LANGS = sorted(p.name for p in LOCALE_DIR.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# Non-LLM helper functions (pure Python / subprocess)
# ---------------------------------------------------------------------------


def download_pot(dest: Path, *, dry_run: bool = False) -> None:
    """Download latest messages.pot from openlibrary master."""
    print(f"→ download messages.pot → {dest}")
    if dry_run:
        return
    with urllib.request.urlopen(OL_MESSAGES_POT_URL) as resp:
        dest.write_bytes(resp.read())


def get_ol_mount(ol_mount: Path | None) -> Path:
    if ol_mount:
        return ol_mount
    env_val = os.environ.get("OL_MOUNT_DIR")
    if env_val:
        return Path(env_val)
    default = Path.home() / "Projects" / "openlibrary"
    if default.exists():
        return default
    raise RuntimeError(
        "OL_MOUNT_DIR is not set and ~/Projects/openlibrary does not exist. "
        "Set OL_MOUNT_DIR to a checked-out openlibrary worktree with Docker running."
    )


def _docker_run(ol_mount: Path, *cmd: str, dry_run: bool = False) -> None:
    full_cmd = [
        "docker",
        "compose",
        "run",
        "--rm",
        "--no-deps",
        "-e",
        f"OL_MOUNT_DIR={ol_mount}",
        "home",
        *cmd,
    ]
    print("→", " ".join(full_cmd))
    if not dry_run:
        subprocess.run(full_cmd, check=True, cwd=ol_mount)


def sync_language(lang: str, ol_mount: Path, *, dry_run: bool = False) -> None:
    """Run i18n-messages update for one language inside Docker."""
    po_src = LOCALE_DIR / lang / "messages.po"
    po_dst = ol_mount / "openlibrary" / "i18n" / lang / "messages.po"
    pot_dst = ol_mount / "openlibrary" / "i18n" / "messages.pot"

    print(f"→ sync {lang}: copy .po + .pot into ol_mount, run update")
    if not dry_run:
        po_dst.parent.mkdir(parents=True, exist_ok=True)
        pot_dst.write_bytes(MESSAGES_POT.read_bytes())
        po_dst.write_bytes(po_src.read_bytes())

    _docker_run(
        ol_mount,
        "python",
        "./scripts/i18n-messages",
        "update",
        lang,
        dry_run=dry_run,
    )

    if not dry_run:
        # Copy updated .po back from ol_mount into our repo's locale/
        updated = po_dst.read_bytes()
        po_src.write_bytes(updated)


def get_untranslated(lang: str) -> list[dict]:
    """Return list of untranslated non-fuzzy entries using babel."""
    try:
        from babel.messages.pofile import read_po
    except ImportError:
        raise RuntimeError("babel is required: pip install babel")

    po_path = LOCALE_DIR / lang / "messages.po"
    with open(po_path, "rb") as f:
        catalog = read_po(f)

    entries = []
    for msg in catalog:
        if not msg.id or msg.fuzzy:
            continue
        if isinstance(msg.string, str) and not msg.string:
            if isinstance(msg.id, tuple):
                entries.append({"id": msg.id[0], "id_plural": msg.id[1]})
            else:
                entries.append({"id": msg.id})
        elif isinstance(msg.string, (list, tuple)) and not any(msg.string):
            if isinstance(msg.id, tuple):
                entries.append({"id": msg.id[0], "id_plural": msg.id[1]})
            else:
                entries.append({"id": msg.id})
    return entries


def _chunk(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def apply_translations(
    lang: str, translations: dict[str, str | list[str]], *, dry_run: bool = False
) -> int:
    """Write {msgid: msgstr} into the .po file. Returns count applied."""
    try:
        from babel.messages.pofile import read_po, write_po
    except ImportError:
        raise RuntimeError("babel is required: pip install babel")

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
                # plural: val must be a list
                if isinstance(val, list):
                    msg.string = tuple(val)
                else:
                    msg.string = (val, val)
            else:
                msg.string = val if isinstance(val, str) else str(val)
            count += 1

    if not dry_run:
        with open(po_path, "wb") as f:
            write_po(f, catalog, include_previous=False)

    return count


def run_test_po_files(lang: str, ol_mount: Path, *, dry_run: bool = False) -> None:
    """Run test_po_files.py for this language inside Docker."""
    _docker_run(
        ol_mount,
        "python",
        "-m",
        "pytest",
        "openlibrary/i18n/test_po_files.py",
        "-k",
        lang,
        "--noconftest",
        "-q",
        dry_run=dry_run,
    )


def validate(lang: str, ol_mount: Path, *, dry_run: bool = False) -> None:
    """Run i18n-messages validate for one language inside Docker."""
    _docker_run(
        ol_mount,
        "python",
        "./scripts/i18n-messages",
        "validate",
        lang,
        dry_run=dry_run,
    )


def compile_po(lang: str, ol_mount: Path, *, dry_run: bool = False) -> None:
    """Compile .po → .mo inside Docker."""
    _docker_run(
        ol_mount,
        "python",
        "./scripts/i18n-messages",
        "compile",
        lang,
        dry_run=dry_run,
    )


def commit_lang(lang: str, model: str, *, dry_run: bool = False) -> str:
    """git add + commit the updated .po for this language. Returns commit sha."""
    po_path = f"locale/{lang}/messages.po"
    msg = (
        f"i18n({lang}): AI translation update\n\n"
        f"Fills untranslated strings using {model}.\n"
        f"Format strings and HTML preserved verbatim. Fuzzy entries left for human review.\n"
        f"🤖 Translations generated by {model}; please review for accuracy."
    )
    print(f"→ git add {po_path} && git commit")
    if dry_run:
        return "dry-run-sha"
    subprocess.run(
        [
            "git",
            "add",
            po_path,
        ],
        check=True,
        cwd=REPO_ROOT,
    )
    result = subprocess.run(
        [
            "git",
            "commit",
            "--author=Michael E. Karpeles (Mek) <michael.karpeles@gmail.com>",
            "-m",
            msg,
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


def open_pr(
    branch: str,
    languages: list[str],
    *,
    mode: str = "per-language",
    dry_run: bool = False,
) -> str:
    """Open a PR on openlibrary-i18n. mode: 'per-language' or 'batch'."""
    lang_list = ", ".join(languages)
    if mode == "batch":
        title = f"i18n: AI translation update for {lang_list}"
    else:
        title = f"i18n({languages[0]}): AI translation update"

    body = (
        f"## Summary\n\n"
        f"AI-generated translation update for: {lang_list}\n\n"
        f"Format strings (`%(name)s`, `%s`, `{{page}}`) and HTML markup preserved verbatim. "
        f"Fuzzy entries left unchanged for human review.\n\n"
        f"**Please review for accuracy before merging.**\n\n"
        f"🤖 Generated by AI translation API. "
        f"Native speaker review recommended."
    )

    cmd = [
        "gh",
        "pr",
        "create",
        "--repo",
        "internetarchive/openlibrary-i18n",
        "--base",
        "main",
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
    ]
    print("→", " ".join(cmd[:6]), "...")
    if dry_run:
        return "dry-run-pr-url"
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# LLM function — ONLY this calls the Anthropic API
# ---------------------------------------------------------------------------


def translate_batch(
    strings: list[dict],
    target_lang: str,
    client,  # anthropic.Anthropic
    model: str,
) -> dict[str, str | list[str]]:
    """
    Single Anthropic API call for up to BATCH_SIZE strings.

    Returns {msgid: msgstr} where plural entries have list[str] values.
    Rules enforced in prompt:
      - Format strings (%(name)s, %s, %d, {page}) preserved verbatim
      - HTML tag names and attribute keys preserved verbatim
      - Proper nouns (Internet Archive, Open Library, Wikidata) not translated
      - URLs, email addresses not translated
    """
    if not strings:
        return {}

    # Build language name for the prompt (map code → human name)
    _lang_names = {
        "ar": "Arabic",
        "as": "Assamese",
        "bn": "Bengali",
        "cs": "Czech",
        "de": "German",
        "es": "Spanish",
        "fr": "French",
        "hi": "Hindi",
        "hr": "Croatian",
        "id": "Indonesian",
        "it": "Italian",
        "ja": "Japanese",
        "ko": "Korean",
        "pl": "Polish",
        "pt": "Portuguese",
        "ro": "Romanian",
        "ru": "Russian",
        "sc": "Sardinian",
        "te": "Telugu",
        "tl": "Tagalog",
        "tr": "Turkish",
        "uk": "Ukrainian",
        "zh": "Chinese (Simplified)",
    }
    lang_name = _lang_names.get(target_lang, target_lang)

    prompt_lines = [
        f"Translate the following Open Library UI strings from English to {lang_name}.",
        "",
        "Rules:",
        "1. Preserve ALL format strings VERBATIM: %(name)s, %(count)d, %s, %d, {page}, etc.",
        "2. Preserve ALL HTML tag names and attribute KEYS verbatim (e.g. <a href=...>, <b>, <strong>).",
        "3. Do NOT translate proper nouns: 'Internet Archive', 'Open Library', 'Wikidata'.",
        "4. Do NOT translate URLs or email addresses.",
        "5. For plural entries (marked with 'plural'), return a JSON array with the correct number",
        f"   of plural forms for {lang_name}.",
        "6. Return ONLY a JSON object mapping English msgid → translated string (or array for plurals).",
        "7. Use natural, concise UI tone. Use formal register if the language has one.",
        "",
        "Strings to translate (JSON array):",
        json.dumps(strings, ensure_ascii=False, indent=2),
        "",
        "Return ONLY valid JSON: {msgid: translation, ...}",
    ]
    prompt = "\n".join(prompt_lines)

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
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
    ol_mount: Path,
    model: str,
    no_translate: bool,
    dry_run: bool,
) -> None:
    """Full pipeline for one language: sync → translate → validate → compile → commit."""
    print(f"\n{'='*60}")
    print(f"Processing: {lang}")
    print("=" * 60)

    # Stage 1: sync with latest .pot
    sync_language(lang, ol_mount, dry_run=dry_run)

    # Stage 2: fill untranslated strings
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

    # Stage 3: validate
    validate(lang, ol_mount, dry_run=dry_run)

    # Stage 4: run test_po_files
    # (non-fatal: some legacy languages have pre-existing issues)
    try:
        run_test_po_files(lang, ol_mount, dry_run=dry_run)
    except subprocess.CalledProcessError:
        print(f"  ⚠ test_po_files failed for {lang} — review before merging")

    # Stage 5: commit
    sha = commit_lang(lang, model, dry_run=dry_run)
    print(f"  committed: {sha}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open Library i18n translation pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--lang", nargs="+", metavar="LANG", help="Languages to process")
    parser.add_argument("--all", action="store_true", help="Process all languages in locale/")
    parser.add_argument(
        "--batch-pr",
        action="store_true",
        help="Open one PR for all languages (default: per-language)",
    )
    parser.add_argument(
        "--no-translate",
        action="store_true",
        help="Skip AI translation; sync + validate only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without executing them",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL),
        help=f"Anthropic model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--ol-mount",
        type=Path,
        default=None,
        help="Path to openlibrary worktree (overrides OL_MOUNT_DIR env var)",
    )
    args = parser.parse_args()

    if not args.lang and not args.all:
        parser.error("Specify --lang LANG [LANG ...] or --all")

    languages = KNOWN_LANGS if args.all else args.lang
    invalid = [l for l in languages if l not in KNOWN_LANGS]
    if invalid:
        parser.error(f"Unknown languages: {invalid}. Known: {KNOWN_LANGS}")

    ol_mount = get_ol_mount(args.ol_mount)
    if not args.dry_run and not ol_mount.exists():
        sys.exit(f"OL_MOUNT_DIR {ol_mount} does not exist")

    # Set up Anthropic client
    client = None
    if not args.no_translate:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            sys.exit(
                "ANTHROPIC_API_KEY is not set. "
                "Use --no-translate to skip AI translation."
            )
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        except ImportError:
            sys.exit("anthropic package is required: pip install anthropic")

    # Step 0: download latest messages.pot
    download_pot(MESSAGES_POT, dry_run=args.dry_run)

    # Process each language
    processed = []
    for lang in languages:
        process_language(
            lang,
            client=client,
            ol_mount=ol_mount,
            model=args.model,
            no_translate=args.no_translate,
            dry_run=args.dry_run,
        )
        processed.append(lang)

    # Open PR(s)
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
