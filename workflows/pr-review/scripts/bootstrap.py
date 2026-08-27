#!/usr/bin/env python3
"""Resolve the repository, the GitHub identity and the worktrees directory.

Runs in whatever directory `conductor run` was invoked from, so its output is
what pins the rest of the workflow to a single repository. Emits a JSON object
on stdout, which Conductor merges into `output.*`.

Identity is reported twice, and the difference matters. `gh_user` is the
account `gh` is active as: the one that will post. `gh_logins` is every account
`gh` holds a working token for on this host, and all of them are you — a review
you left last month under one login is still yours when you come back signed in
as another. Matching prior comments against the active login alone makes an
earlier review invisible and sends the workflow down the first-pass path,
posting a second opinion on top of your own.

Only the host this repository lives on is considered. Logins are per-host, so a
`jane` on an enterprise host is not the `jane` who could have commented on a
github.com pull request, and folding the two together would credit you with a
stranger's review.

Usage:
    bootstrap.py

Output:
    ok, error, repo_root, repo_name, name_with_owner, host, default_branch,
    gh_user, gh_logins, worktrees_dir
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from urllib.parse import urlparse


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))
    sys.exit(0)


def run(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(args, capture_output=True, text=True)  # noqa: S603
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def fail(message: str) -> None:
    emit(
        {
            "ok": False,
            "error": message,
            "repo_root": "",
            "repo_name": "",
            "name_with_owner": "",
            "host": "",
            "default_branch": "",
            "gh_user": "",
            "gh_logins": [],
            "worktrees_dir": "",
        }
    )


def logins_on(host: str, active: str) -> list[str]:
    """Every login `gh` holds a working token for on `host`, active one first.

    Degrades to just the active login rather than failing: an unreadable
    account list is a narrower search, not a wrong one, and the run is still
    worth doing. `--json` is only understood by newer `gh`, and a non-zero exit
    here also means "one of the accounts needs re-authenticating", which is not
    this workflow's problem to report.
    """
    ordered = [active] if active else []
    seen = {active.casefold()} if active else set()

    rc, out, _ = run(["gh", "auth", "status", "--json", "hosts"])
    if rc != 0 or not out:
        return ordered
    try:
        hosts = json.loads(out).get("hosts")
    except json.JSONDecodeError:
        return ordered
    if not isinstance(hosts, dict):
        return ordered

    for name, accounts in hosts.items():
        if str(name).casefold() != host.casefold() or not isinstance(accounts, list):
            continue
        for account in accounts:
            if not isinstance(account, dict) or account.get("state") != "success":
                continue
            login = str(account.get("login") or "").strip()
            if not login or login.casefold() in seen:
                continue
            seen.add(login.casefold())
            ordered.append(login)
    return ordered


def main() -> None:
    rc, repo_root, err = run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0 or not repo_root:
        fail(f"Not inside a git repository (cwd {os.getcwd()}). {err}")

    rc, out, err = run(["gh", "repo", "view", "--json", "nameWithOwner,url"])
    if rc != 0 or not out:
        fail(f"'gh repo view' failed - check the GitHub CLI is installed and authenticated. {err}")
    try:
        repo = json.loads(out)
    except json.JSONDecodeError as exc:
        fail(f"'gh repo view' did not return JSON: {exc}")
    nwo = str(repo.get("nameWithOwner") or "")
    if not nwo:
        fail("'gh repo view' returned no repository name.")
    host = urlparse(str(repo.get("url") or "")).hostname or "github.com"

    rc, gh_user, err = run(["gh", "api", "user", "--jq", ".login"])
    if rc != 0 or not gh_user:
        fail(f"'gh api user' failed - run 'gh auth status'. {err}")

    rc, ref, _ = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
    default_branch = ref.split("/", 1)[1] if rc == 0 and "/" in ref else ""
    if not default_branch:
        rc, branch, _ = run(
            ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"]
        )
        default_branch = branch if rc == 0 else ""
    if not default_branch:
        fail(f"Could not determine the default branch for {nwo}.")

    repo_name = os.path.basename(repo_root)
    emit(
        {
            "ok": True,
            "error": "",
            "repo_root": repo_root,
            "repo_name": repo_name,
            "name_with_owner": nwo,
            "host": host,
            "default_branch": default_branch,
            "gh_user": gh_user,
            "gh_logins": logins_on(host, gh_user),
            "worktrees_dir": os.path.join(os.path.dirname(repo_root), repo_name + ".worktrees"),
        }
    )


if __name__ == "__main__":
    main()
