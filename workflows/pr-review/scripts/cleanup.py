#!/usr/bin/env python3
"""Remove an owned, clean review checkout from its running workflow."""

from __future__ import annotations

import json
import sys

from worktree_lifecycle import cleanup_owned


def main(argv: list[str]) -> None:
    if len(argv) != 3 or not all(argv):
        print(json.dumps({
            "ok": False, "worktree_removed": False, "branch_deleted": False,
            "worktree_path": argv[1] if len(argv) > 1 else "",
            "notes": "cleanup.py requires repo_root, owned worktree_path and branch; nothing deleted.",
        }))
        return
    print(json.dumps(cleanup_owned(*argv)))


if __name__ == "__main__":
    main(sys.argv[1:])
