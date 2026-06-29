# openlibrary-i18n

Canonical home for [Open Library](https://openlibrary.org) translation files.

This repository holds the `.po` translation files for all supported languages, the `messages.pot` template, translation tooling, and CI validation tests. It is the single source of truth for OL's localized strings.

## Language coverage

23 active languages: ar, as, bn, cs, de, es, fr, hi, hr, id, it, ja, ko, pl, pt, ro, ru, sc, te, tl, tr, uk, zh.

## Repository structure

```
openlibrary-i18n/
  locale/
    de/messages.po
    es/messages.po
    ...  (one directory per language)
  messages.pot                 # source string template (downloaded from openlibrary)
  scripts/
    translate.py               # translation pipeline: sync → AI gap-fill → validate → compile
  tests/
    test_po_files.py           # HTML structure parity validation
    validators.py              # format-string validation
  .github/workflows/
    validate-pr.yml            # blocks PRs that fail test_po_files.py
```

## How translations work

1. When the source strings in `openlibrary` change, `messages.pot` is updated automatically by the `generate-pot` pre-commit hook.
2. A GitHub Actions workflow downloads the latest `messages.pot`, runs `i18n-messages update` for each language, fills translation gaps using an AI translation API, validates the result, and opens a PR.
3. PRs that introduce format-string mismatches or broken HTML in translations are blocked by CI.

## Contributing translations

To improve a translation for your language, edit the relevant `locale/{lang}/messages.po` file and open a pull request. CI will verify your changes automatically.

## Related

- [Open Library](https://github.com/internetarchive/openlibrary) — main application repo
- [i18n extraction pipeline epic #13061](https://github.com/internetarchive/openlibrary/issues/13061)
