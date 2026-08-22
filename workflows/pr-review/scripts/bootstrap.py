#!/usr/bin/env python3
"""Resolve the repository, the GitHub identity and the worktrees directory.

Runs in whatever directory `conductor run` was invoked from, so its output is
what pins the rest of the workflow to a single repository. Emits a JSON object
on stdout, which Conductor merges into `output.*`.

Usage:
    bootstrap.py

Output:
    ok, error, repo_root, repo_name, name_with_owner, default_branch, gh_user,
    worktrees_dir
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


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
            "default_branch": "",
            "gh_user": "",
            "worktrees_dir": "",
        }
    )


def main() -> None:
    rc, repo_root, err = run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0 or not repo_root:
        fail(f"Not inside a git repository (cwd {os.getcwd()}). {err}")

    rc, nwo, err = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    if rc != 0 or not nwo:
        fail(f"'gh repo view' failed - check the GitHub CLI is installed and authenticated. {err}")

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
            "default_branch": default_branch,
            "gh_user": gh_user,
            "worktrees_dir": os.path.join(os.path.dirname(repo_root), repo_name + ".worktrees"),
        }
    )


if __name__ == "__main__":
    main()
