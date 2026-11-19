# openlibrary-i18n

Where [Open Library](https://openlibrary.org)'s translations are maintained.

This repository holds the `.po` translation file for each language, the `messages.pot` template they are built from, translation tooling, and validation tests. Starting with [openlibrary#13070](https://github.com/internetarchive/openlibrary/pull/13070), the Open Library production image copies each `locale/<lang>/messages.po` from here over the matching `openlibrary/i18n/<lang>/messages.po` in the main repository. So **translation changes belong here**: an edit to one of those files in the main repository is overwritten at build. The one exception: a language whose file here fails the build's safety check keeps the main repository's file for that build.

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
    sync-pot.yml                    # commits messages.pot from openlibrary master
    validate-pr.yml                 # runs the checks on PRs
```

## How translations work

1. English strings are extracted into `openlibrary/i18n/messages.pot` in the [main repository](https://github.com/internetarchive/openlibrary) by its `generate-pot` pre-commit hook.
2. When that file changes on `master`, the main repository's `trigger-i18n.yml` sends a `repository_dispatch` event here. `sync-pot.yml` then commits the new `messages.pot` straight to `main`.
3. `translate.yml` runs on that event (or on a manual `workflow_dispatch`; it has no schedule). It downloads the new `messages.pot` and groups the languages that have untranslated strings into batches. Each batch then runs an AI translation step on a clean checkout of `main`, following `i18n-translation-instructions.md`. The fresh `messages.pot` is passed to that step outside the checkout, through the `I18N_POT` environment variable. The step syncs only its own languages from it, fills untranslated strings and opens one PR per batch. The workflow then merges those PRs itself.
4. `validate-pr.yml` runs `tests/test_po_files.py` and the format-string validators on PRs that touch `locale/` or `messages.pot`, and reports the result on the PR. It is meant to check only the languages a PR changes, but today it checks every locale: changed-language detection fails in the shallow checkout (`no merge base`), and both steps fall back to the full suite. See #106, which also covers a second bug that the fallback currently hides. **`main` has no branch protection, so a failing check does not prevent a merge.** Review is what catches a bad translation.
5. `weblate-review.yml` runs on every translation PR from a person: Weblate's sync PR, a contributor's fork, or a staff branch. An AI agent scans the diff for abuse (spam, offensive text, prompt injection, broken placeholders) and posts its verdict as a PR comment. It is not a quality review. Only Weblate's sync PR is then merged automatically; see below.

## What merges automatically

`main` has no branch protection, so the workflows below are what decide whether a PR lands without a person.

| PR | Where it comes from | Merged by | Human review |
|---|---|---|---|
| `i18n/ai-*` | `translate.yml`'s AI translation run | `translate.yml` right after the run, or `reconcile.yml` every 6 hours, once the `validate` and "Verify translations deploy cleanly" checks pass | None |
| `weblate` (branch in this repo) | Weblate pushing its translators' work | `weblate-review.yml`, after the abuse review, a translation-only file check, and its own repair and deploy checks. Weblate gets stuck if its PR sits open, so it is never left waiting for a person | None on GitHub |
| Anything else that touches `locale/` (forks, staff branches) | People | A maintainer | **Yes**: the bot only posts its abuse-review comment |

A PR labelled `no-automation` when it is opened skips `weblate-review.yml` entirely, with no review comment either. A label added later doesn't stop a run already under way.

## Contributing translations

To improve a translation for your language, edit `locale/<lang>/messages.po` and open a pull request against `main`. A bot posts an automated abuse-review comment, and then a maintainer reviews and merges your PR. If you translate on Weblate instead, your work reaches `main` through Weblate's own sync PR. Before you open a PR, run the checks locally (Python with `babel` and `pytest`):

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
