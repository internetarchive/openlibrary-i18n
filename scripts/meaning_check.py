#!/usr/bin/env python3
"""Deterministic half of the meaning check (#331).

Every other check here is structural: placeholders, HTML, plural counts. None asks
whether a translation *means* what the English says, and every bug in #301, #310,
#313 and #316 passed them all. meaning-check.yml has an agent judge that, and this
script does everything around the agent, so the agent never touches GitHub:

  extract  Collect the translations to check into a JSON file for the agent:
           the entries a PR added or changed (--base REF), or a whole language's
           translations for a one-time sweep (--all --lang X). Capped at --max,
           sampled evenly across the list, so a huge PR stays affordable.
  report   Turn the agent's findings file into a Markdown PR comment. Findings are
           untrusted data: only ones naming an entry that was actually checked are
           kept, and every field is length-limited and escaped.

The check is report-only. Neither command fails because of what the agent found.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIR = REPO_ROOT / "locale"
MAX_ROWS = 50  # findings shown in one comment
FIELD_LIMIT = 300  # characters kept from any agent-written field


def _read_po(data: bytes):
    from babel.messages.pofile import read_po
    return read_po(BytesIO(data))


def _is_translated(string) -> bool:
    if isinstance(string, (tuple, list)):
        return any(string)
    return bool(string)


def _entry(lang: str, index: int, msg) -> dict:
    plural = isinstance(msg.id, tuple)
    entry = {
        "key": f"{lang}:{index}",
        "lang": lang,
        "msgid": msg.id[0] if plural else msg.id,
    }
    if plural:
        entry["msgid_plural"] = msg.id[1]
        entry["translation"] = list(msg.string)
    else:
        entry["translation"] = msg.string
    if msg.auto_comments:
        entry["context"] = " ".join(msg.auto_comments)
    if msg.locations:
        entry["used_in"] = sorted({loc for loc, _ in msg.locations})[:3]
    return entry


def changed_entries(base_cat, head_cat, lang: str) -> list[dict]:
    """Translated, non-fuzzy entries in head whose text differs from base (or that
    base lacks). base_cat None means the language is new: every translation counts."""
    base = {}
    if base_cat is not None:
        base = {(m.id, m.context): m.string for m in base_cat if m.id}
    out = []
    for i, msg in enumerate(m for m in head_cat if m.id):
        if msg.fuzzy or not _is_translated(msg.string):
            continue
        old = base.get((msg.id, msg.context))
        if old is not None and _as_tuple(old) == _as_tuple(msg.string):
            continue
        out.append(_entry(lang, i, msg))
    return out


def all_entries(cat, lang: str) -> list[dict]:
    """Every translated, non-fuzzy entry: the one-time sweep."""
    return [
        _entry(lang, i, msg)
        for i, msg in enumerate(m for m in cat if m.id)
        if not msg.fuzzy and _is_translated(msg.string)
    ]


def _as_tuple(string) -> tuple:
    return tuple(string) if isinstance(string, (list, tuple)) else (string,)


def sample(entries: list[dict], limit: int) -> list[dict]:
    """At most `limit` entries, spread evenly over the list (deterministic), so a
    capped run still covers every language in it. limit <= 0 means no cap."""
    if limit <= 0 or len(entries) <= limit:
        return entries
    step = len(entries) / limit
    return [entries[int(i * step)] for i in range(limit)]


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True, capture_output=True).stdout


def _changed_langs(base: str) -> list[str]:
    out = _git("diff", "--name-only", base, "--", "locale/*/messages.po").decode()
    return sorted({p.split("/")[1] for p in out.split() if p.endswith("/messages.po")})


def extract(base: str | None, lang: str | None, limit: int) -> dict:
    entries: list[dict] = []
    if base is not None:
        merge_base = _git("merge-base", base, "HEAD").decode().strip()
        for code in _changed_langs(merge_base):
            path = LOCALE_DIR / code / "messages.po"
            if not path.exists():
                continue  # language deleted: nothing to check
            try:
                base_cat = _read_po(_git("show", f"{merge_base}:locale/{code}/messages.po"))
            except subprocess.CalledProcessError:
                base_cat = None  # new language
            entries += changed_entries(base_cat, _read_po(path.read_bytes()), code)
    else:
        entries = all_entries(_read_po((LOCALE_DIR / lang / "messages.po").read_bytes()), lang)
    chosen = sample(entries, limit)
    return {"mode": "pr" if base is not None else "sweep",
            "total": len(entries), "checked": len(chosen), "entries": chosen}


def write_entries(doc: dict, out: Path) -> None:
    """One entry per line, so the agent can read the file in chunks by line."""
    lines = ",\n".join(json.dumps(e, ensure_ascii=False) for e in doc["entries"])
    mode = json.dumps(doc.get("mode", "pr"))
    out.write_text(
        f'{{"mode": {mode}, "total": {doc["total"]}, "checked": {doc["checked"]}, "entries": [\n{lines}\n]}}\n',
        encoding="utf-8",
    )


def _cell(value, limit: int = FIELD_LIMIT) -> str:
    """Make untrusted text safe inside a Markdown table cell."""
    if isinstance(value, list):
        value = " / ".join(str(v) for v in value)
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "'").replace("<", "&lt;").replace(">", "&gt;").replace("@", "@​")


def load_findings(path: Path, entries: list[dict]) -> list[dict] | None:
    """The agent's findings, keeping only ones that name a checked entry. None if
    the file is missing or unreadable (the agent did not finish)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return None
    by_key = {e["key"]: e for e in entries}
    findings, seen = [], set()
    for f in raw:
        if not isinstance(f, dict) or f.get("key") not in by_key or f["key"] in seen:
            continue
        seen.add(f["key"])
        findings.append({
            "entry": by_key[f["key"]],
            "back_translation": str(f.get("back_translation", "")),
            "problem": str(f.get("problem", "")),
        })
    return findings


def render(doc: dict, findings: list[dict] | None) -> str:
    checked, total = doc["checked"], doc["total"]
    what = "existing translations" if doc.get("mode") == "sweep" else "new or changed translations"
    scope = (f"{checked} of {total}" if checked < total else f"all {total}") + f" {what}"
    head = "### Meaning check (report-only)\n\n"
    if findings is None:
        return head + (f"The meaning check did not finish, so the {scope} "
                       "were not checked. This does not block the PR.\n")
    if not findings:
        return head + f"Checked {scope}. None look wrong.\n"
    lines = [
        head + f"Checked {scope}. "
        f"**{len(findings)} may not mean what the English says.** "
        "A model flagged these; it can be wrong, especially for short strings. "
        "This does not block the PR.\n",
        "| Lang | English | Translation | Back-translation | Problem |",
        "|---|---|---|---|---|",
    ]
    for f in findings[:MAX_ROWS]:
        e = f["entry"]
        lines.append(
            f"| {_cell(e['lang'], 10)} | {_cell(e['msgid'], 120)} | {_cell(e['translation'], 120)} "
            f"| {_cell(f['back_translation'], 120)} | {_cell(f['problem'], 200)} |"
        )
    if len(findings) > MAX_ROWS:
        lines.append(f"\n…and {len(findings) - MAX_ROWS} more; see the workflow run's artifact.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract", help="Write the entries to check as JSON")
    which = ex.add_mutually_exclusive_group(required=True)
    which.add_argument("--base", help="Check entries changed since this ref (a PR)")
    which.add_argument("--all", action="store_true", help="Check every translation of --lang (sweep)")
    ex.add_argument("--lang", help="Language for --all")
    ex.add_argument("--max", type=int, default=400, help="Most entries to check; 0 = no cap")
    ex.add_argument("--out", type=Path, required=True)
    rp = sub.add_parser("report", help="Print the Markdown comment for the findings")
    rp.add_argument("--entries", type=Path, required=True)
    rp.add_argument("--findings", type=Path, required=True)
    args = ap.parse_args(argv)

    if args.cmd == "extract":
        if args.all and not args.lang:
            ap.error("--all needs --lang")
        if args.all and not (LOCALE_DIR / args.lang / "messages.po").exists():
            ap.error(f"no locale/{args.lang}/messages.po")
        doc = extract(None if args.all else args.base, args.lang, args.max)
        write_entries(doc, args.out)
        print(f"{doc['checked']} of {doc['total']} entries to check")
        return 0

    doc = json.loads(args.entries.read_text(encoding="utf-8"))
    sys.stdout.write(render(doc, load_findings(args.findings, doc["entries"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
