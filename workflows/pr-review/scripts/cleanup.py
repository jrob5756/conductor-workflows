#!/usr/bin/env python3
"""Remove the review worktree and its branch.

Runs from the repository root, never from the worktree it is deleting.

Cleanup failures are reported but never fail the run: by the time this executes
the review has already been posted, and leaving a stale directory behind is not
worth turning a successful review into a failed workflow. The next run for the
same PR rebuilds the worktree from scratch anyway.

Usage:
    cleanup.py <repo_root> <worktree_path> <branch>

Output:
    ok, worktree_removed, branch_deleted, notes
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys


def run(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(args, capture_output=True, text=True)  # noqa: S603
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main(argv: list[str]) -> None:
    repo_root = argv[0] if argv else ""
    worktree_path = argv[1] if len(argv) > 1 else ""
    branch = argv[2] if len(argv) > 2 else ""

    notes: list[str] = []
    worktree_removed = False
    branch_deleted = False

    if not repo_root or not os.path.isdir(repo_root):
        print(
            json.dumps(
                {
                    "ok": True,
                    "worktree_removed": False,
                    "branch_deleted": False,
                    "notes": "No repository root was given, so there was nothing to clean up.",
                }
            )
        )
        return

    if worktree_path:
        rc, _, err = run(["git", "-C", repo_root, "worktree", "remove", "--force", worktree_path])
        worktree_removed = rc == 0
        if not worktree_removed and os.path.isdir(worktree_path):
            shutil.rmtree(worktree_path, ignore_errors=True)
            worktree_removed = not os.path.isdir(worktree_path)
            if not worktree_removed:
                notes.append(f"Could not remove the worktree at {worktree_path}. {err}")
        run(["git", "-C", repo_root, "worktree", "prune"])

    if branch:
        rc, _, err = run(["git", "-C", repo_root, "branch", "-D", branch])
        branch_deleted = rc == 0
        if not branch_deleted:
            notes.append(f"Could not delete the branch {branch}. {err}")

    print(
        json.dumps(
            {
                "ok": True,
                "worktree_removed": worktree_removed,
                "branch_deleted": branch_deleted,
                "notes": " ".join(notes),
            }
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
