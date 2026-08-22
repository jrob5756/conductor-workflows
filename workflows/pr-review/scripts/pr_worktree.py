#!/usr/bin/env python3
"""Check a pull request out into a throwaway git worktree.

The worktree is read-only as far as this workflow is concerned - nothing is
ever written to it - so the checkout is rebuilt from scratch on every run
rather than reconciled. That makes the step idempotent: a worktree or branch
left behind by an interrupted run is removed and recreated, not worked around.

`pull/<n>/head` is fetched rather than the head branch, which is what makes
this work identically for a fork PR and a same-repository one.

Usage:
    pr_worktree.py <repo_root> <worktrees_dir> <pr_number> <name_with_owner> <base_ref>

Output:
    ok, error, worktree_path, branch, base_ref, remote, head_sha
"""

from __future__ import annotations

import json
import os
import shutil
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
            "worktree_path": "",
            "branch": "",
            "base_ref": "",
            "remote": "",
            "head_sha": "",
        }
    )


def resolve_remote(repo_root: str, name_with_owner: str) -> str:
    """Find the remote pointing at the PR's repository, preferring origin."""
    rc, out, _ = run(["git", "-C", repo_root, "remote", "-v"])
    if rc != 0:
        return "origin"
    needle = name_with_owner.lower()
    matches: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1].lower()
        if url.endswith(".git"):
            url = url[: -len(".git")]
        if url.endswith(needle) and name not in matches:
            matches.append(name)
    if "origin" in matches:
        return "origin"
    return matches[0] if matches else "origin"


def discard_existing(repo_root: str, worktree_path: str, branch: str) -> None:
    """Remove a worktree and branch left over from an earlier run."""
    run(["git", "-C", repo_root, "worktree", "prune"])
    if os.path.isdir(worktree_path):
        run(["git", "-C", repo_root, "worktree", "remove", "--force", worktree_path])
    if os.path.isdir(worktree_path):
        shutil.rmtree(worktree_path, ignore_errors=True)
        run(["git", "-C", repo_root, "worktree", "prune"])
    # Deleted only after the worktree is gone; git refuses while one holds it.
    if run(["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])[0] == 0:
        run(["git", "-C", repo_root, "branch", "-D", branch])


def main(argv: list[str]) -> None:
    if len(argv) < 4:
        fail("pr_worktree.py requires repo_root, worktrees_dir, pr_number and name_with_owner")

    repo_root, worktrees_dir, pr_number, name_with_owner = argv[0], argv[1], argv[2], argv[3]
    base_ref = argv[4] if len(argv) > 4 else ""

    if not pr_number.isdigit():
        fail(f"Pull request number is not numeric: {pr_number!r}")

    branch = f"pr-review/{pr_number}"
    worktrees_dir = os.path.abspath(os.path.expanduser(worktrees_dir))
    worktree_path = os.path.join(worktrees_dir, f"pr-{pr_number}")
    remote = resolve_remote(repo_root, name_with_owner)

    discard_existing(repo_root, worktree_path, branch)
    os.makedirs(worktrees_dir, exist_ok=True)

    rc, _, err = run(
        [
            "git",
            "-C",
            repo_root,
            "fetch",
            "--force",
            remote,
            f"pull/{pr_number}/head:{branch}",
        ]
    )
    if rc != 0:
        fail(
            f"Could not fetch pull/{pr_number}/head from remote '{remote}'. "
            f"Check the PR number and your access to {name_with_owner}. {err}"
        )

    # The base branch is needed later to diff the PR. A shallow or partial
    # clone may not have it, and a stale remote-tracking ref moves the merge
    # base backwards, which quietly widens the reviewed diff with commits the
    # pull request never touched.
    if base_ref:
        rc, _, err = run(["git", "-C", repo_root, "fetch", "--force", remote, base_ref])
        if rc != 0:
            fail(
                f"Could not fetch the base branch '{base_ref}' from remote '{remote}'. "
                f"Reviewing against a stale base would show changes this pull request "
                f"did not make. {err}"
            )

    rc, _, err = run(["git", "-C", repo_root, "worktree", "add", worktree_path, branch])
    if rc != 0:
        fail(f"Could not create a worktree at {worktree_path}. {err}")

    rc, head_sha, err = run(["git", "-C", worktree_path, "rev-parse", "HEAD"])
    if rc != 0 or not head_sha:
        fail(f"Worktree at {worktree_path} is not a usable git checkout. {err}")

    emit(
        {
            "ok": True,
            "error": "",
            "worktree_path": worktree_path,
            "branch": branch,
            "base_ref": base_ref,
            "remote": remote,
            "head_sha": head_sha,
        }
    )


if __name__ == "__main__":
    main(sys.argv[1:])
