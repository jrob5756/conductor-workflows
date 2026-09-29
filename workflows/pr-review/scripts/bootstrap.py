#!/usr/bin/env python3
"""Resolve the invoking repository and automatically select a writable GitHub identity."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from urllib.parse import urlparse

from github_auth import AuthError, select_account


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))
    sys.exit(0)


def run(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def fail(message: str, *, auth_error: bool = False) -> None:
    emit({
        "ok": False, "auth_error": auth_error, "error": message, "repo_root": "",
        "repo_name": "", "name_with_owner": "", "host": "", "default_branch": "",
        "gh_user": "", "gh_logins": [], "auth_source": "", "auth_notice": "",
        "worktrees_dir": "",
    })


def repository_from_remote(remote: str) -> tuple[str, str]:
    if "://" not in remote:
        remote = "ssh://" + remote.replace(":", "/", 1)
    parsed = urlparse(remote)
    nwo = parsed.path.strip("/").removesuffix(".git")
    if not parsed.hostname or not re.fullmatch(r"[\w.-]+/[\w.-]+", nwo):
        raise ValueError("The origin remote must name a GitHub owner/repository.")
    return parsed.hostname, nwo


def main() -> None:
    try:
        rc, repo_root, err = run(["git", "rev-parse", "--show-toplevel"])
        if rc != 0 or not repo_root:
            fail(f"Not inside a git repository (cwd {os.getcwd()}). {err}")

        rc, out, _ = run(["gh", "repo", "view", "--json", "nameWithOwner,url"])
        repo = json.loads(out) if rc == 0 else {}
        if isinstance(repo, dict) and repo.get("nameWithOwner") and repo.get("url"):
            host = urlparse(repo["url"]).hostname
            nwo = repo["nameWithOwner"]
        else:
            rc, remote, _ = run(["git", "remote", "get-url", "origin"])
            if rc != 0:
                fail("Could not determine the target GitHub repository from origin.")
            host, nwo = repository_from_remote(remote)
        if not host:
            fail("Could not determine the target GitHub host.")

        identity, repo = select_account(host, nwo)
        default_branch = repo.get("default_branch")
        canonical = repo.get("full_name")
        if not isinstance(default_branch, str) or not default_branch or not isinstance(canonical, str) or not canonical:
            fail("GitHub returned incomplete repository metadata.")
        repo_name = os.path.basename(repo_root)
        emit({
            "ok": True, "auth_error": False, "error": "", "repo_root": repo_root,
            "repo_name": repo_name, "name_with_owner": canonical, "host": host,
            "default_branch": default_branch, **identity,
            "worktrees_dir": os.path.join(os.path.dirname(repo_root), repo_name + ".worktrees"),
        })
    except AuthError as exc:
        fail(str(exc), auth_error=True)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        fail(f"Repository preflight failed: {exc}")


if __name__ == "__main__":
    main()
