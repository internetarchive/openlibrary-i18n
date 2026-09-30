# openlibrary-i18n

Where [Open Library](https://openlibrary.org)'s translations are maintained.

This repository holds the `.po` translation file for each language, the `messages.pot` template they are built from, translation tooling, and validation tests. Starting with [openlibrary#13070](https://github.com/internetarchive/openlibrary/pull/13070), the Open Library production image copies each `locale/<lang>/messages.po` from here over the matching `openlibrary/i18n/<lang>/messages.po` in the main repository. So **translation changes belong here**: an edit to one of those files in the main repository is overwritten at build.

Until [openlibrary#13070](https://github.com/internetarchive/openlibrary/pull/13070) ships, the site still reads the main repository's files, so a change made here reaches openlibrary.org when it ships, not before. That delay is expected, and the change is not lost.

The default branch is `main`.

## Language coverage

23 languages: ar, as, bn, cs, de, es, fr, hi, hr, id, it, ja, ko, pl, pt, ro, ru, sc, te, tl, tr, uk, zh.

## Repository structure

```
openlibrary-i18n/
  locale/
    de/messages.po
    de/messages.mo
    es/messages.po
    ...  (one directory per language)
  messages.pot                      # source string template, copied from openlibrary master
  i18n                              # toolbox: ./i18n pull | sync | incomplete | batch | untranslated | apply | fix | validate | test | compile
  i18n-translation-instructions.md  # instructions the AI translation run follows
  requirements.txt
  tests/
    test_po_files.py                # HTML structure parity validation
    validators.py                   # format-string validation
    test_i18n.py                    # tests for the ./i18n toolbox
  .github/workflows/
    translate.yml                   # AI translation run
    validate-pr.yml                 # runs the checks on PRs
```

## How translations work

1. English strings are extracted into `openlibrary/i18n/messages.pot` in the [main repository](https://github.com/internetarchive/openlibrary) by its `generate-pot` pre-commit hook.
2. When that file changes on `master`, the main repository's `trigger-i18n.yml` sends a `repository_dispatch` event here.
3. `translate.yml` runs on that event (or on a manual `workflow_dispatch`; it has no schedule). It downloads the new `messages.pot`, updates every `locale/<lang>/messages.po` from it (`./i18n pull --sync`), and then an AI translation step, following `i18n-translation-instructions.md`, fills untranslated strings and opens a PR per batch of languages. The workflow then merges those PRs itself.
4. `validate-pr.yml` runs `tests/test_po_files.py` and the format-string validators on PRs that touch `locale/` or `messages.pot`, and reports the result on the PR. **`main` has no branch protection, so a failing check does not prevent a merge.** Review is what catches a bad translation.

## Contributing translations

To improve a translation for your language, edit `locale/<lang>/messages.po` and open a pull request against `main`. Before you do, run the checks locally (Python with `babel` and `pytest`):

```sh
./i18n validate <lang>   # %-style placeholders such as %(name)s must match the English
./i18n test <lang>       # HTML tags in each translation must match the English
```

`./i18n validate` only partly checks plural entries (`msgstr[0]`, `msgstr[1]`, ...): it catches a wrong placeholder name, but not a placeholder missing from one plural form. Check each form by eye.

If a translation is correct but its HTML has to differ from the English, start its `msgstr` with `<!-- i18n-lint no-tree-equal -->` to skip the HTML check for that entry. (The main repository's check also accepts `<!-- i18n-lint no-tree-order -->`, which allows tags in a different order. The check here does not support it yet.)

Review `fuzzy` entries one at a time, and never clear `fuzzy` flags in bulk: a fuzzy entry's `msgstr` is often a translation of a different string, and only the flag keeps it off the site.

To add a new language, [open an issue](https://github.com/internetarchive/openlibrary-i18n/issues) first; it needs changes in both repositories.

Full translator guide: https://docs.openlibrary.org/everyone/internationalization.html

## Related

- [Open Library](https://github.com/internetarchive/openlibrary): main application repo
- [i18n extraction pipeline epic #13061](https://github.com/internetarchive/openlibrary/issues/13061)
