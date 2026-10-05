# Weblate for openlibrary-i18n

Self-hosted [Weblate](https://weblate.org/) as a contributor-facing translation
UI on top of [`internetarchive/openlibrary-i18n`](https://github.com/internetarchive/openlibrary-i18n),
the canonical store for Open Library's `.po` translations. Weblate lets volunteers
and native speakers add and correct strings through a web UI (with translation
memory, glossary, suggestions, and per-string review) instead of hand-editing
`.po` files and opening PRs by hand.

This directory is the **first-milestone deliverable** of
[openlibrary#13802](https://github.com/internetarchive/openlibrary/issues/13802)
(origin idea: openlibrary-i18n#113; epic: openlibrary#13061): a working **local
stand-up** on a single machine plus a proven **Weblate → repo round-trip**. It is
not a production deployment.

> **Status:** draft for review. The compose + docs here are runnable today against
> a local git remote. Pointing Weblate at the real GitHub repo is a documented,
> one-credential step (see [Real-repo wiring](#real-repo-wiring)) that is Mek's to
> authorize — it needs a scoped PAT this stand-up deliberately does not create.

---

## What's here

| File | Purpose |
|---|---|
| `docker-compose.yml` | Base stack: Weblate + PostgreSQL + Valkey (Redis). Adapted from the [official Weblate compose](https://github.com/WeblateOrg/docker-compose): the weblate image is pinned, a loopback port is published, and restart policy is `unless-stopped`. The read-only containers + tmpfs are upstream defaults (boot-tested here). |
| `docker-compose.localtest.yml` | Override that mounts a local dir of **bare git repos** into the container, so a component can use a `file://` URL as a GitHub stand-in. This is how the integration test below was run — no credentials, no real-repo writes. |
| `environment.example` | Config template. Copy to `environment` (gitignored) and edit. |
| `setup_component.py` | Reproducible project+component creation via the REST API: sets `branch=main`, `push_branch=weblate`, `merge_style=rebase`, `filemask=locale/*/messages.po`. Defaults to the `github` backend (opens PRs) for the real repo; pass `OLI_VCS=git` for the local `file://` test (push-only, no PR). |

---

## Quick start (local)

```bash
cd weblate
cp environment.example environment        # set WEBLATE_ADMIN_PASSWORD, etc.
docker compose up -d                      # first boot runs DB migrations (~1-2 min)
# wait for HTTP 200:
until curl -sf -o /dev/null http://localhost:8098/; do sleep 3; done
open http://localhost:8098/               # log in as admin
```

Then add a component pointing at a git repo holding `locale/<lang>/messages.po`
(see [Real-repo wiring](#real-repo-wiring) for the GitHub case, or the integration
test for the local `file://` case).

Tear down (removes containers **and** volumes):

```bash
docker compose down -v
```

---

## The branch model (the load-bearing decision)

Both Weblate (human edits) and the daily `translate.yml` AI run write the same
files — `locale/<lang>/messages.po` on `openlibrary-i18n`. The rule that keeps
them from fighting:

> **Weblate commits to branches OUTSIDE the `i18n/` namespace — prefix `weblate`
> — and never writes `main` directly. It opens PRs for human review.**

This is not a style preference; it is forced by two mechanisms in the live
pipeline. Both were re-confirmed against `translate.yml` and
`i18n-translation-instructions.md` at the commit this work builds on,
`3c5982b` (`openlibrary-i18n@main`):

**1. The AI run auto-merges `i18n/ai-*` PRs (hard, enforced in the workflow).**
`translate.yml`'s merge step selects open PRs by head-branch prefix and squash-merges
the ones whose required checks pass:

```yaml
# .github/workflows/translate.yml (3c5982b), lines 113-116, 133
gh pr list \
  --state open \
  --json number,headRefName \
  --jq '.[] | select(.headRefName | startswith("i18n/ai-")) | .number' \
| while IFS= read -r pr; do
    ...
    gh pr merge "$pr" --squash --delete-branch ...
```

The repo has `allow_auto_merge:false` and `main` has **no branch protection**, so
(as the workflow's own comment says) "nothing but this gate stops unreviewed
machine output from landing." A Weblate PR on an `i18n/ai-*` head would be treated
as machine output and **squash-merged to `main` unreviewed**. Never use that prefix.

**2. Any `i18n/*` PR counts toward the AI run's backlog stop (soft, instruction-level).**
The AI agent is instructed to halt when ≥5 such PRs are open:

```bash
# i18n-translation-instructions.md (3c5982b), lines 40-41
OPEN=$(gh pr list --state open --json headRefName \
  --jq '[.[] | select(.headRefName | startswith("i18n/"))] | length')
```
> "Max 5 open i18n PRs. If ≥ 5 when you check, open a GitHub issue noting the
> backlog and stop." (line 332)

So a handful of open Weblate PRs under `i18n/*` would **silently pause daily
translation** for everyone.

**`weblate` / `weblate/<lang>` avoids both:** it doesn't match `i18n/ai-` (never
auto-merged) and doesn't match `i18n/` (never counted), it only ever reaches `main`
through a human-reviewed PR, and — because it's a distinct namespace — the two
systems' branches never collide.

### Coexistence over time

- **The AI run only fills *empty/untranslated* msgids** (`_untranslated_from_catalog`
  uses `any(msg.string)`), and merges to `main` frequently. Weblate carries human
  corrections and additions on a long-lived `weblate` branch.
- **Weblate must rebase from `main` regularly** so it absorbs AI merges. Set the
  component's merge style to *rebase* and enable periodic repository updates (or a
  push webhook). Note: `translate.yml` merges with `github.token`, and pushes made
  with that token **start no workflows**, so Weblate cannot rely on a push event to
  learn about AI merges — it must poll/update on a schedule.
- **Genuine same-msgid conflicts are adjudicated by the human reviewing the Weblate
  PR** — which is exactly the right place for human-over-AI precedence.

---

## Real-repo wiring

To point this stand-up at the real `internetarchive/openlibrary-i18n` and have
Weblate open pull requests, one credential is required. **This stand-up does not
create or use it** — it is Mek's to issue.

**1. Issue a fine-grained PAT, scoped to one repository.**
- Repository access: **only** `internetarchive/openlibrary-i18n`.
- Permissions: **Contents: Read and write**, **Pull requests: Read and write**,
  **Metadata: Read-only**. Nothing else; nothing org-wide.
- This is entirely separate from the pipeline secrets (`OL_BOT_PAT`,
  `CLAUDE_CODE_OAUTH_TOKEN`) and does not touch `translate.yml`.

**2. Give it to Weblate** (in `environment`):
```ini
WEBLATE_GITHUB_USERNAME=openlibrary-bot      # the account the PAT belongs to
WEBLATE_GITHUB_TOKEN=<the fine-grained PAT>
```
Weblate uses these to open PRs via the GitHub API. **Set these before creating the
component:** the `github` ("GitHub pull request") VCS backend only registers once
GitHub credentials are configured (verified against Weblate 2026.10:
`GitMergeRequestBase.is_configured()` returns `bool(get_credentials_configuration())`).
Without them, selecting that backend fails because it isn't available. (Weblate
2026.10 also offers a `github-app` backend that authenticates as a GitHub App
instead of a PAT; this stand-up uses the simpler PAT path.)

**3. Configure the component** (UI → *Manage → Repository maintenance*, or the API):
- **Source code repository:** `https://github.com/internetarchive/openlibrary-i18n.git`
- **Repository branch:** `main`
- **Repository push URL:** `https://github.com/internetarchive/openlibrary-i18n.git`
- **Push branch:** `weblate` (or `weblate/<lang>` for per-language components)
- **File mask:** `locale/*/messages.po`
- **File format:** `gettext PO file`
- **New translation base:** `messages.pot`
- **Merge style:** `Rebase`
- **Pull request / push style:** `GitHub pull request`
- Turn **off** committing straight to the upstream branch; Weblate pushes to
  `weblate` and opens a PR against `main`.

`setup_component.py` automates this: with its default `OLI_VCS=github` it creates
the component on the `github` backend with `push_branch=weblate` and
`merge_style=rebase`, so a run against the real repo opens PRs rather than pushing
directly. (The `github` backend is what makes it a *pull request*; the plain `git`
backend used by the local test only pushes the branch.)

With that, a Weblate edit → commit on `weblate` → **pull request to `main`**, which
a maintainer reviews and merges, exactly as the branch model prescribes.

---

## Integration test — Weblate → repo round-trip

**Run on this Mac mini, 2026-10-04**, Weblate `2026.10`, against a **local bare
clone** of `openlibrary-i18n@main` (`3c5982b`) used as a GitHub stand-in — no
GitHub credentials, no writes to the real repo. It proves the mechanism the real
deployment relies on: *edit → commit → push to a non-`main` branch*.

**Setup**
- `docker compose -f docker-compose.yml -f docker-compose.localtest.yml up -d`
  with `WEBLATE_LOCAL_REPOS_DIR` pointing at a dir holding
  `openlibrary-i18n.git` (a `git init --bare` seeded with `main` at `3c5982b`).
- Test-only setting `WEBLATE_VCS_ALLOW_SCHEMES=https,ssh,file` — Weblate blocks
  `file://` by default (`VCS_ALLOW_SCHEMES` defaults to `https,ssh`). **The real
  GitHub deployment does not need this**; it uses `https`.
- Component (via the API, equivalent to the UI *Add component* form):
  `repo=file:///repos/openlibrary-i18n.git`, `branch=main`,
  **`push_branch=weblate`**, `filemask=locale/*/messages.po`, `file_format=po`.
  Weblate discovered all 24 languages (2042 source strings in `fr`).

**Edit** — the `fr` unit for source `Settings` was changed to a sentinel target
`Paramètres — WEBLATE_ROUNDTRIP_20261004` (`PATCH /api/units/16337/`, state
*translated*), then `commit` and `push` via the repository API.

**Result — verified against the bare repo (`git`, not Weblate's word):**

| Check | Expected | Observed |
|---|---|---|
| Branch Weblate pushed to | `weblate`, not `main` | `weblate` created at `c3e617c` |
| `main` after push | unchanged `3c5982b` | `3c5982b` ✓ |
| Commit on `weblate` | one l10n commit | `chore(l10n): update French translation` |
| Sentinel in `locale/fr/messages.po` | on `weblate` only | `weblate`: 1, `main`: 0 ✓ |
| Files changed (`main..weblate`) | only the one `.po` | `locale/fr/messages.po`, +7/-6 |

```
$ git -C <bare> branch
* main
  weblate
$ git -C <bare> rev-parse main        # == seeded SHA, untouched
3c5982b92ed773e215e1662490b92d94c7837411
$ git -C <bare> log --oneline main..weblate
c3e617c chore(l10n): update French translation
```

**Two Weblate behaviours the review of a real Weblate PR should expect** (from the
actual diff, not inferred):
- **Minimal diffs — no mass rewrap.** Only the edited entry and the PO header
  changed; the rest of the file kept its wrapping. This matters: `pybabel update`
  (the pipeline's `./i18n sync`) rewraps at 76 columns, which would turn a
  one-string change into a whole-file diff. Weblate did **not** do that here, so
  Weblate edits stay reviewable and rarely conflict line-for-line with AI merges.
- **Header normalisation.** Weblate rewrote `Last-Translator`, `Language-Team`
  (now a Weblate URL), added `X-Generator`, and **canonicalised `Plural-Forms`**
  from `nplurals=2; plural=(n >= 2);` to `nplurals=2; plural=n > 1;`. For integer
  `n` these select identically (`n ≥ 2` ⇔ `n > 1`), so there is no runtime change
  for `fr`; but any Weblate-touched file carries a `Plural-Forms` rewrite, which a
  reviewer (and the `bake-regression` gate) should read from the *compiled*
  catalog, per the i18n-pipeline notes, rather than flag as a regression.

**What this test does NOT cover** (needs the scoped PAT — see
[Real-repo wiring](#real-repo-wiring)): Weblate opening an actual **GitHub pull
request** against `main`, and the GitHub-side merge/rebase cycle. The git-level
round-trip (the risky part — that it never touches `main`) is proven; the PR layer
is a GitHub API feature configured by `WEBLATE_GITHUB_TOKEN` + "GitHub pull
request" push style.

---

## Running behind a reverse proxy / TLS (verified)

The compose publishes the UI on **127.0.0.1 only**, so reaching it from another
machine means putting it behind a reverse proxy that terminates TLS (an nginx/Caddy
front end, or a tunnel such as `cloudflared tunnel --url http://localhost:8098`).
Three `environment` settings are then **required** — the first two are not optional,
and omitting them produces a login that looks broken:

```ini
WEBLATE_SITE_DOMAIN=your-public-host.example.com   # the public hostname, no scheme
WEBLATE_ENABLE_HTTPS=1
WEBLATE_SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https
```

**Why both of the last two matter (verified against a `cloudflared` quick tunnel,
2026-10-05):** the browser sends an `https://…` `Origin`, but the proxy forwards the
request to Weblate over plain `http` internally. Without
`WEBLATE_SECURE_PROXY_SSL_HEADER`, Django computes `request.scheme == "http"`, so its
CSRF origin check compares the `https` Origin against an `http` expected origin, they
don't match, and **every login (and every edit POST) 403s** with
`CSRF verification failed`. Setting the proxy-SSL header makes `request.scheme` resolve
to `https` (from the `X-Forwarded-Proto` the proxy sends), the origins match, and login
`302`s normally. `WEBLATE_SITE_DOMAIN` must be the public host so Weblate trusts that
origin and builds correct absolute URLs.

Recreate the `weblate` container after changing these (`docker compose up -d weblate`).
A `cloudflared` quick tunnel is **ephemeral** — the `*.trycloudflare.com` URL changes
whenever the tunnel restarts and dies with the process; a stable URL means a named
Cloudflare tunnel (account + DNS) or your own proxy with a real certificate. See the
official [`docker-compose-https.yml`](https://github.com/WeblateOrg/docker-compose) for
a built-in TLS front end.

## Security / operational notes for a real deployment
- `environment` holds secrets and is gitignored. Set/rotate
  `WEBLATE_ADMIN_PASSWORD`, `POSTGRES_PASSWORD` (the template ships a weak
  `weblate` default), and the PAT if they ever land anywhere shared.
- Keep `WEBLATE_REGISTRATION_OPEN=0` until an auth model (who may translate) is
  decided — a later milestone.
- Images are pinned (`weblate/weblate:2026.10`, `postgres:18-alpine`,
  `valkey/valkey:9.1.2`). Bump deliberately.
