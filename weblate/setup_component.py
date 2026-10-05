#!/usr/bin/env python3
"""Create the Open Library project + Website component in a running Weblate.

Uses the Weblate REST API (stdlib only) — this is exactly the call the
integration test in README.md used, parameterised so the same script works for
the real GitHub deployment. Idempotent-ish: skips creation on HTTP 400 "already
exists".

Get the admin token once the stack is up:

  docker compose exec -T weblate weblate shell -c \
    "from weblate.auth.models import User; print(User.objects.get(username='admin').auth_token.key)"

Then, for the real repo:

  WEBLATE_URL=http://localhost:8098 \
  WEBLATE_TOKEN=wlu_... \
  OLI_REPO=https://github.com/internetarchive/openlibrary-i18n.git \
  python3 setup_component.py

For the local file:// integration test, set OLI_REPO/OLI_PUSH to
file:///repos/openlibrary-i18n.git (and WEBLATE_VCS_ALLOW_SCHEMES=https,ssh,file).
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("WEBLATE_URL", "http://localhost:8098").rstrip("/")
TOKEN = os.environ["WEBLATE_TOKEN"]
REPO = os.environ.get(
    "OLI_REPO", "https://github.com/internetarchive/openlibrary-i18n.git"
)
PUSH = os.environ.get("OLI_PUSH", REPO)
BRANCH = os.environ.get("OLI_BRANCH", "main")
# The branch model: push OUTSIDE the i18n/ namespace so the AI pipeline never
# auto-merges or counts Weblate PRs. See README.md.
PUSH_BRANCH = os.environ.get("OLI_PUSH_BRANCH", "weblate")


def call(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{BASE}/api{path}", data=data, method=method)
    req.add_header("Authorization", f"Token {TOKEN}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or "{}")


def ensure(label: str, status: int, body: dict) -> None:
    if status in (200, 201):
        print(f"  ok: {label}")
    elif status == 400 and "already" in json.dumps(body).lower():
        print(f"  exists: {label}")
    else:
        sys.exit(f"  FAILED {label}: HTTP {status} {json.dumps(body)[:400]}")


def main() -> None:
    ensure(
        "project openlibrary",
        *call(
            "POST",
            "/projects/",
            {"name": "Open Library", "slug": "openlibrary", "web": "https://openlibrary.org/"},
        ),
    )
    ensure(
        "component website",
        *call(
            "POST",
            "/projects/openlibrary/components/",
            {
                "name": "Website",
                "slug": "website",
                "vcs": "git",
                "repo": REPO,
                "push": PUSH,
                "branch": BRANCH,
                "push_branch": PUSH_BRANCH,
                "file_format": "po",
                "filemask": "locale/*/messages.po",
                "new_base": "messages.pot",
                "template": "",
            },
        ),
    )
    print(f"\nComponent configured: {REPO} ({BRANCH}) -> push branch '{PUSH_BRANCH}'.")


if __name__ == "__main__":
    main()
