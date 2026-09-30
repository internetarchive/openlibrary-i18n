#!/usr/bin/env python3
"""
Bake-regression check: if olbase baked this repo today, which strings that
openlibrary currently shows translated would render in English, and which
baked msgstrs have broken %-placeholders?

  ./i18n bake-regression [--openlibrary-ref master] [--ref HEAD] [--json out.json]

What "baking" means here is openlibrary#13070: at image build, olbase copies
locale/<lang>/messages.po from this repo over openlibrary/i18n/<lang>/messages.po
for every <lang> directory present here, then openlibrary compiles each catalog
with babel's write_mo() at its default use_fuzzy=False. So both sides are
compiled with write_mo() and loaded with babel.support.Translations -- the same
calls openlibrary makes -- and asked for each string, rather than inferring
from msgstr text what gettext would do.

Findings, per language:
  regression           openlibrary's compiled catalog holds a translation for
                       (msgctxt, msgid), at some n for plurals, and the baked
                       catalog compiles to the English there: the entry is
                       missing, fuzzy, empty, or the plural form n selects is
                       empty or absent.
  placeholder          a non-fuzzy baked msgstr (any plural form) whose
                       %-placeholders differ from the English it stands in for.
                       Gates only when openlibrary's version of the same entry
                       does not have the defect; the absolute count is reported.
  plural_rule          a Plural-Forms rule that can never select one of its
                       declared forms. Reported, not gating.
  bake_error           a locale here with no openlibrary/i18n/<lang>/ directory:
                       the Dockerfile's cp has no destination and the build fails.

Only msgids in openlibrary's messages.pot at the compared ref are considered;
translations of msgids no template requests are never rendered.

Exit status: 0 no gating findings, 1 gating findings (with --baseline, only
those not in the baseline),
2 the check could not run (fetch failure, empty input). An empty openlibrary
side is treated as an error, never as "no regressions".
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path

from babel.messages.catalog import Catalog, Message
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import Translations

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENLIBRARY_REPO = "internetarchive/openlibrary"
OPENLIBRARY_GIT = f"https://github.com/{OPENLIBRARY_REPO}"
RAW_URL = "https://raw.githubusercontent.com/{repo}/{sha}/openlibrary/i18n/{path}"

# Large enough that every plural rule in CLDR has shown each of its forms.
PLURAL_PROBE_LIMIT = 1000

_FALLBACK = "\x00bake-regression-fallback\x00"

_CFMT_RE = re.compile(
    r"""
    %(?:
        (?:\([a-zA-Z_][a-zA-Z0-9_]*?\))?
        (?:[-+0 #]{0,5})
        (?:\d+|\*)?
        (?:\.(?:\d+|\*))?
        (?:h|l|ll|w|I|I32|I64)?
        [cCdiouxXeEfgGaAnpsSZ]
    )
    |%%
    """,
    re.VERBOSE,
)


class CheckError(Exception):
    """The check could not produce a result; never report this as a pass."""


# ---------------------------------------------------------------------------
# Rendering: compile and load exactly as openlibrary does
# ---------------------------------------------------------------------------

class _Fallback:
    """Returned by Translations when the catalog has no usable entry."""

    def gettext(self, message):
        return _FALLBACK

    def ngettext(self, msgid1, msgid2, n):
        return _FALLBACK

    def pgettext(self, context, message):
        return _FALLBACK

    def npgettext(self, context, msgid1, msgid2, n):
        return _FALLBACK


def compile_catalog(catalog: Catalog) -> Translations:
    """write_mo() with openlibrary's arguments, loaded the way openlibrary loads it."""
    buf = BytesIO()
    write_mo(buf, catalog)
    buf.seek(0)
    translations = Translations(buf)
    translations.add_fallback(_Fallback())
    return translations


def _lookup(t: Translations, msgctxt, msgid, msgid_plural, n) -> str:
    """The .mo value openlibrary's ugettext/ungettext would get, or _FALLBACK."""
    if msgid_plural is None:
        return t.pgettext(msgctxt, msgid) if msgctxt else t.gettext(msgid)
    if msgctxt:
        return t.npgettext(msgctxt, msgid, msgid_plural, n)
    return t.ngettext(msgid, msgid_plural, n)


def renders_translation(t: Translations, msgctxt, msgid, msgid_plural, n) -> bool:
    """
    True when openlibrary would show text other than the English source.

    openlibrary uses `translations.ugettext(s) or s` and, for plurals,
    `translations.ungettext(...)` falling back to English on a falsy value.
    write_mo() also fills an empty plural form with the English msgid or
    msgid_plural, so a non-empty .mo string can still be English.
    """
    value = _lookup(t, msgctxt, msgid, msgid_plural, n)
    return bool(value) and value not in (_FALLBACK, msgid, msgid_plural)


def has_translation(
    t: Translations, message: Message | None, msgctxt, msgid, msgid_plural, n,
) -> bool:
    """
    True when the compiled catalog holds a translation the translator wrote.

    Presence comes from the .mo lookup, so whatever write_mo() dropped (fuzzy,
    empty, a singular/plural shape the caller does not ask for) is absent. The
    .po `message` is consulted only to recognise a plural form write_mo() filled
    with the English, since that text is otherwise indistinguishable from a
    translation identical to the English.
    """
    value = _lookup(t, msgctxt, msgid, msgid_plural, n)
    if not value or value == _FALLBACK:
        return False
    if msgid_plural is not None and message is not None and not isinstance(message.string, str):
        form = t.plural(n)
        if form >= len(message.string) or not message.string[form]:
            return False
    return True


def plural_probes(*translations: Translations) -> list[int]:
    """The smallest n selecting each plural form, under every catalog's rule."""
    probes = {1}
    for t in translations:
        seen = set()
        for n in range(PLURAL_PROBE_LIMIT):
            form = t.plural(n)
            if form not in seen:
                seen.add(form)
                probes.add(n)
    return sorted(probes)


def forms_selected(catalog: Catalog) -> dict[int, list[int]]:
    """plural form index -> the n values in [0, PLURAL_PROBE_LIMIT) selecting it."""
    t = compile_catalog(catalog)
    forms: dict[int, list[int]] = {}
    for n in range(PLURAL_PROBE_LIMIT):
        forms.setdefault(t.plural(n), []).append(n)
    return forms


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    kind: str
    lang: str
    msgctxt: str | None
    msgid: str
    msgid_plural: str | None = None
    reason: str = ""
    n: list[int] = field(default_factory=list)
    # False when openlibrary's msgstr is identical to the English, so losing it
    # changes nothing on the page.
    visible: bool = True
    # Only gating findings set the exit status. A placeholder defect that
    # openlibrary already ships in the same entry is reported but does not gate.
    gating: bool = True
    openlibrary_msgstr: str | list[str] | None = None
    baked_msgstr: str | list[str] | None = None

    def key(self) -> tuple:
        return (self.kind, self.lang, self.msgctxt or "", self.msgid)


def _split_id(message: Message) -> tuple[str, str | None]:
    if isinstance(message.id, (list, tuple)):
        return message.id[0], message.id[1]
    return message.id, None


def _jsonable(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    return value


def _why_english(message: Message | None, n: int | None) -> str:
    if message is None:
        return "missing"
    if message.fuzzy:
        return "fuzzy"
    if isinstance(message.string, (list, tuple)):
        if not any(message.string):
            return "empty"
        # read_po pads a short msgstr list to nplurals, so an absent form is an empty one.
        return "empty-plural-form"
    if not message.string:
        return "empty"
    if n is not None:
        return "not-plural"
    return "unknown"


def find_regressions(
    lang: str,
    openlibrary: Catalog,
    baked: Catalog,
    live_keys: set[tuple[str | None, str]],
) -> list[Finding]:
    ol_t = compile_catalog(openlibrary)
    baked_t = compile_catalog(baked)
    probes = plural_probes(ol_t, baked_t)
    findings = []
    for message in openlibrary:
        if not message.id:
            continue
        msgid, msgid_plural = _split_id(message)
        if (message.context, msgid) not in live_keys:
            continue
        ns = [None] if msgid_plural is None else probes
        baked_message = baked.get(msgid, context=message.context)
        lost = [
            n for n in ns
            if has_translation(ol_t, message, message.context, msgid, msgid_plural, n)
            and not has_translation(baked_t, baked_message, message.context, msgid, msgid_plural, n)
        ]
        if not lost:
            continue
        findings.append(Finding(
            kind="regression",
            lang=lang,
            msgctxt=message.context,
            msgid=msgid,
            msgid_plural=msgid_plural,
            reason=_why_english(baked_message, lost[0]),
            n=[n for n in lost if n is not None],
            visible=any(
                renders_translation(ol_t, message.context, msgid, msgid_plural, n)
                and not renders_translation(baked_t, message.context, msgid, msgid_plural, n)
                for n in lost
            ),
            openlibrary_msgstr=_jsonable(message.string),
            baked_msgstr=_jsonable(baked_message.string) if baked_message else None,
        ))
    return findings


def placeholder_signature(s: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """Named specifiers as a set; positional ones in order, since order binds arguments."""
    specs = [m.group(0) for m in _CFMT_RE.finditer(s) if m.group(0) != "%%"]
    return (
        frozenset(x for x in specs if x.startswith("%(")),
        tuple(x for x in specs if not x.startswith("%(")),
    )


def find_placeholder_defects(
    lang: str,
    baked: Catalog,
    live_keys: set[tuple[str | None, str]],
) -> list[Finding]:
    """
    Every non-empty form of a non-fuzzy msgstr must carry the placeholders of
    the English it replaces. A plural form selected only by n=1 may match either
    msgid or msgid_plural; a form selected by any other n replaces msgid_plural,
    so it must match msgid_plural. With nplurals=1 the single form is selected by
    every n, which is the case #15 describes. A form selected only by n=0 may
    also carry no placeholders, as Arabic's zero form does ("no books").
    """
    forms = forms_selected(baked)
    no_placeholders = (frozenset(), ())
    findings = []
    for message in baked:
        if not message.id or message.fuzzy or not message.string:
            continue
        msgid, msgid_plural = _split_id(message)
        if (message.context, msgid) not in live_keys:
            continue
        if msgid_plural is None:
            if isinstance(message.string, str) and (
                placeholder_signature(message.string) != placeholder_signature(msgid)
            ):
                findings.append(Finding(
                    kind="placeholder", lang=lang, msgctxt=message.context, msgid=msgid,
                    reason="msgstr placeholders differ from msgid",
                    baked_msgstr=message.string,
                ))
            continue
        strings = message.string if isinstance(message.string, (list, tuple)) else (message.string,)
        singular_sig = placeholder_signature(msgid)
        plural_sig = placeholder_signature(msgid_plural)
        bad_forms = []
        bad_n: list[int] = []
        for index, form in enumerate(strings):
            if not form:
                continue
            selected_by = forms.get(index, [])
            allowed = {plural_sig} if set(selected_by) - {1} else {singular_sig, plural_sig}
            if set(selected_by) == {0}:
                allowed.add(no_placeholders)
            if placeholder_signature(form) not in allowed:
                bad_forms.append(index)
                bad_n.extend(n for n in selected_by[:3])
        if bad_forms:
            findings.append(Finding(
                kind="placeholder", lang=lang, msgctxt=message.context, msgid=msgid,
                msgid_plural=msgid_plural,
                reason="msgstr[%s] placeholders differ from msgid_plural"
                       % ",".join(map(str, bad_forms)),
                n=sorted(set(bad_n)),
                baked_msgstr=list(strings),
            ))
    return findings


def find_plural_rule_defects(lang: str, baked: Catalog) -> list[Finding]:
    """Plural forms the compiled catalog's rule can never select, for any n tried."""
    unreachable = sorted(set(range(baked.num_plurals)) - set(forms_selected(baked)))
    if not unreachable:
        return []
    header = dict(baked.mime_headers).get("Plural-Forms") or baked.plural_forms
    return [Finding(
        kind="plural_rule", lang=lang, msgctxt=None, msgid="",
        reason=(f"Plural-Forms '{header}' never selects msgstr"
                f"[{','.join(map(str, unreachable))}] for n < {PLURAL_PROBE_LIMIT}"),
        gating=False,
    )]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _parse(data: bytes, what: str) -> Catalog:
    try:
        return read_po(BytesIO(data))
    except Exception as e:
        raise CheckError(f"cannot parse {what}: {e}") from e


class OpenlibrarySource:
    """openlibrary/i18n at a commit, fetched over HTTPS; no checkout needed."""

    def __init__(self, ref: str, repo: str = OPENLIBRARY_REPO):
        self.repo = repo
        self.ref = ref
        self.sha = resolve_github_ref(repo, ref)

    def describe(self) -> dict:
        return {"repo": self.repo, "ref": self.ref, "sha": self.sha}

    def fetch(self, path: str) -> bytes | None:
        url = RAW_URL.format(repo=self.repo, sha=self.sha, path=path)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if attempt == 2:
                    raise CheckError(f"GET {url}: HTTP {e.code}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 2:
                    raise CheckError(f"GET {url}: {e}") from e
        raise AssertionError("unreachable")

    def languages(self) -> list[str] | None:
        """Locale directories, or None if the listing is unavailable (API rate limit)."""
        url = (f"https://api.github.com/repos/{self.repo}/contents/openlibrary/i18n"
               f"?ref={self.sha}")
        request = urllib.request.Request(url, headers=_github_headers())
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                entries = json.load(resp)
        except (urllib.error.URLError, TimeoutError, ValueError):
            return None
        return sorted(e["name"] for e in entries if e.get("type") == "dir")


class DirectorySource:
    """A directory laid out like openlibrary/i18n: messages.pot and <lang>/messages.po."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_dir():
            raise CheckError(f"{self.path} is not a directory")

    def describe(self) -> dict:
        return {"dir": str(self.path)}

    def fetch(self, path: str) -> bytes | None:
        p = self.path / path
        return p.read_bytes() if p.exists() else None

    def languages(self) -> list[str]:
        return sorted(p.name for p in self.path.iterdir() if (p / "messages.po").exists())


class GitRefSource:
    """This repo's locale/ at a git ref, read with git show (no checkout of the ref)."""

    def __init__(self, ref: str, repo_root: Path = REPO_ROOT):
        self.ref = ref
        self.repo_root = repo_root
        self.sha = self._git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()

    def _git(self, *args) -> bytes:
        proc = subprocess.run(["git", "-C", str(self.repo_root), *args], capture_output=True)
        if proc.returncode:
            raise CheckError(f"git {' '.join(args)}: {proc.stderr.decode().strip()}")
        return proc.stdout

    def describe(self) -> dict:
        return {"ref": self.ref, "sha": self.sha}

    def languages(self) -> list[str]:
        names = self._git("ls-tree", "--full-tree", "--name-only", f"{self.sha}:locale").decode().split()
        return sorted(
            name for name in names
            if subprocess.run(
                ["git", "-C", str(self.repo_root), "cat-file", "-e",
                 f"{self.sha}:locale/{name}/messages.po"],
                capture_output=True,
            ).returncode == 0
        )

    def fetch(self, path: str) -> bytes:
        return self._git("show", f"{self.sha}:locale/{path}")


class LocaleDirSource(DirectorySource):
    """A locale/ directory on disk, for checking uncommitted work."""


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def resolve_github_ref(repo: str, ref: str) -> str:
    """Full commit SHA for a branch, tag or SHA, so the report names an immutable commit."""
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    proc = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{repo}", ref, f"refs/heads/{ref}",
         f"refs/tags/{ref}^{{}}"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            sha, name = line.split("\t")
            if name in (ref, f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}"):
                return sha
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/commits/{ref}", headers=_github_headers(),
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            return json.load(resp)["sha"]
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
        raise CheckError(f"cannot resolve {repo}@{ref}: {e}") from e


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def live_keys_from_pot(pot: Catalog) -> set[tuple[str | None, str]]:
    return {(m.context, _split_id(m)[0]) for m in pot if m.id}


def check_language(lang: str, openlibrary: Catalog, baked: Catalog, live: set) -> dict:
    regressions = find_regressions(lang, openlibrary, baked, live)
    placeholders = find_placeholder_defects(lang, baked, live)
    already_shipped = {f.key() for f in find_placeholder_defects(lang, openlibrary, live)}
    for f in placeholders:
        f.gating = f.key() not in already_shipped
    ol_t = compile_catalog(openlibrary)
    probes = plural_probes(ol_t)
    translated = 0
    for m in openlibrary:
        if not m.id:
            continue
        msgid, msgid_plural = _split_id(m)
        if (m.context, msgid) not in live:
            continue
        ns = [None] if msgid_plural is None else probes
        if any(has_translation(ol_t, m, m.context, msgid, msgid_plural, n) for n in ns):
            translated += 1
    return {
        "openlibrary_translated": translated,
        "regressions": regressions,
        "placeholder_defects": placeholders,
        "plural_rule_defects": find_plural_rule_defects(lang, baked),
    }


def run(openlibrary_source, baked_source, baseline: dict | None = None) -> dict:
    pot_bytes = openlibrary_source.fetch("messages.pot")
    if not pot_bytes:
        raise CheckError("openlibrary messages.pot not found at the compared ref")
    live = live_keys_from_pot(_parse(pot_bytes, "openlibrary messages.pot"))
    if not live:
        raise CheckError("openlibrary messages.pot has no msgids")

    langs = baked_source.languages()
    if not langs:
        raise CheckError("no locale/<lang>/messages.po found on the baked side")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ol_files = dict(zip(langs, pool.map(
            lambda lang: openlibrary_source.fetch(f"{lang}/messages.po"), langs)))

    languages: dict[str, dict] = {}
    bake_errors: list[Finding] = []
    for lang in langs:
        ol_bytes = ol_files[lang]
        if ol_bytes is None:
            bake_errors.append(Finding(
                kind="bake_error", lang=lang, msgctxt=None, msgid="",
                reason=f"openlibrary/i18n/{lang}/ does not exist; the olbase cp has no destination",
            ))
            continue
        result = check_language(
            lang,
            _parse(ol_bytes, f"openlibrary {lang}/messages.po"),
            _parse(baked_source.fetch(f"{lang}/messages.po"), f"baked {lang}/messages.po"),
            live,
        )
        languages[lang] = result

    if not any(r["openlibrary_translated"] for r in languages.values()):
        raise CheckError("openlibrary renders no translations at all; the comparison is not live")

    ol_langs = openlibrary_source.languages()
    not_baked = None if ol_langs is None else sorted(set(ol_langs) - set(langs))

    findings = bake_errors + [
        f for r in languages.values()
        for f in r["regressions"] + r["placeholder_defects"] + r["plural_rule_defects"]
    ]
    baseline_keys = None
    if baseline is not None:
        baseline_keys = {
            (f["kind"], f["lang"], f["msgctxt"] or "", f["msgid"])
            for f in baseline.get("findings", [])
        }
    new = [f for f in findings
           if f.gating and (baseline_keys is None or f.key() not in baseline_keys)]

    return {
        "openlibrary": openlibrary_source.describe(),
        "baked": baked_source.describe(),
        "live_msgids": len(live),
        "totals": {
            "regressions": sum(len(r["regressions"]) for r in languages.values()),
            "placeholder_defects_new": sum(
                1 for r in languages.values() for f in r["placeholder_defects"] if f.gating),
            "placeholder_defects": sum(len(r["placeholder_defects"]) for r in languages.values()),
            "plural_rule_defects": sum(len(r["plural_rule_defects"]) for r in languages.values()),
            "bake_errors": len(bake_errors),
            "gating": sum(1 for f in findings if f.gating),
            "new_findings": len(new) if baseline is not None else None,
        },
        "languages": {
            lang: {
                "openlibrary_translated": r["openlibrary_translated"],
                "regressions": len(r["regressions"]),
                "placeholder_defects_new": sum(1 for f in r["placeholder_defects"] if f.gating),
                "placeholder_defects": len(r["placeholder_defects"]),
            }
            for lang, r in languages.items()
        },
        "not_baked": not_baked,
        "findings": [asdict(f) for f in findings],
        "new_findings": [asdict(f) for f in new] if baseline is not None else None,
        "status": "fail" if new else "pass",
    }


def format_report(report: dict) -> str:
    lines = []
    ol, baked, totals = report["openlibrary"], report["baked"], report["totals"]
    lines.append(f"openlibrary: {ol.get('repo', '')}@{ol.get('sha') or ol.get('dir')}"
                 + (f" ({ol['ref']})" if ol.get("ref") else ""))
    lines.append(f"baked:       openlibrary-i18n@{baked.get('sha') or baked.get('dir')}"
                 + (f" ({baked['ref']})" if baked.get("ref") else ""))
    lines.append(f"live msgids in openlibrary messages.pot: {report['live_msgids']}")
    lines.append("")
    lines.append(f"{'lang':<6}{'ol translated':>15}{'regressions':>13}"
                 f"{'placeholder new':>17}{'placeholder all':>17}")
    for lang, r in report["languages"].items():
        lines.append(f"{lang:<6}{r['openlibrary_translated']:>15}{r['regressions']:>13}"
                     f"{r['placeholder_defects_new']:>17}{r['placeholder_defects']:>17}")
    lines.append("")
    by_reason: dict[str, int] = {}
    for f in report["findings"]:
        if f["kind"] == "regression":
            by_reason[f["reason"]] = by_reason.get(f["reason"], 0) + 1
    invisible = sum(1 for f in report["findings"]
                    if f["kind"] == "regression" and not f["visible"])
    lines.append(f"regressions to English: {totals['regressions']}"
                 + (f"  ({', '.join(f'{k} {v}' for k, v in sorted(by_reason.items()))})"
                    if by_reason else ""))
    if invisible:
        lines.append(f"  of which {invisible} lose a msgstr identical to the English,"
                     " so the page does not change")
    lines.append(f"placeholder defects not already in openlibrary: "
                 f"{totals['placeholder_defects_new']}")
    lines.append(f"bake errors:            {totals['bake_errors']}")
    lines.append(f"gating total:           {totals['gating']}")
    lines.append("")
    lines.append("reported, not gating:")
    lines.append(f"  placeholder defects, all baked msgstrs: {totals['placeholder_defects']}"
                 f" ({totals['placeholder_defects'] - totals['placeholder_defects_new']}"
                 " also shipped by openlibrary today)")
    for f in report["findings"]:
        if f["kind"] == "plural_rule":
            lines.append(f"  plural rule, {f['lang']}: {f['reason']}")
    if report["not_baked"] is None:
        lines.append("openlibrary-only locales: unknown (directory listing unavailable)")
    elif report["not_baked"]:
        lines.append(f"openlibrary-only locales, left as they are by the bake: "
                     f"{', '.join(report['not_baked'])}")
    if totals["new_findings"] is not None:
        lines.append(f"not in baseline:        {totals['new_findings']}")
    status = report["status"].upper()
    if totals["new_findings"] is not None:
        status += (f" on findings not in the baseline; {totals['gating']} gating findings"
                   " in total, including the baseline's")
    lines.append(f"status: {status}")
    return "\n".join(lines)


def format_findings(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        ctx = f"[{f['msgctxt']}] " if f["msgctxt"] else ""
        n = f" n={f['n']}" if f["n"] else ""
        gate = "" if f["gating"] else " (not gating)"
        lines.append(f"{f['kind']:<12} {f['lang']:<4} {f['reason']}{n}{gate}: {ctx}{f['msgid']!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bake-regression",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ol = parser.add_mutually_exclusive_group()
    ol.add_argument("--openlibrary-ref", default="master",
                    help="openlibrary branch, tag or SHA to compare against (default: master)")
    ol.add_argument("--openlibrary-dir", type=Path,
                    help="a local directory laid out like openlibrary/i18n, instead of fetching")
    baked = parser.add_mutually_exclusive_group()
    baked.add_argument("--ref", default="HEAD",
                       help="git ref of this repo to bake (default: HEAD)")
    baked.add_argument("--locale-dir", type=Path,
                       help="a locale/ directory on disk, e.g. uncommitted work")
    parser.add_argument("--json", type=Path, metavar="PATH",
                        help="write the machine-readable report here ('-' for stdout)")
    parser.add_argument("--baseline", type=Path, metavar="PATH",
                        help="a previous --json report; exit 1 only on findings not in it")
    parser.add_argument("--list", action="store_true", help="print every finding")
    args = parser.parse_args(argv)

    try:
        ol_source = (DirectorySource(args.openlibrary_dir) if args.openlibrary_dir
                     else OpenlibrarySource(args.openlibrary_ref))
        baked_source = (LocaleDirSource(args.locale_dir) if args.locale_dir
                        else GitRefSource(args.ref))
        baseline = json.loads(args.baseline.read_text()) if args.baseline else None
        report = run(ol_source, baked_source, baseline)
    except CheckError as e:
        print(f"bake-regression: ERROR, the check did not run: {e}", file=sys.stderr)
        return 2

    out = sys.stderr if args.json and str(args.json) == "-" else sys.stdout
    if args.list:
        print(format_findings(report["findings"]), file=out)
        print(file=out)
    print(format_report(report), file=out)
    if args.json:
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if str(args.json) == "-":
            print(text)
        else:
            args.json.write_text(text + "\n", encoding="utf-8")
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
