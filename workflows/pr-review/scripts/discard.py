#!/usr/bin/env python3
"""Explicitly discard a stopped review's owned checkout, invalidating its checkpoints."""

from __future__ import annotations

import argparse
import json
import sys

from worktree_lifecycle import cleanup_owned


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root")
    parser.add_argument("worktree_path", help="Exact owned pr-N-<identity> checkout path, not a PR number")
    parser.add_argument(
        "--acknowledge-discard", action="store_true", required=True,
        help="Acknowledge existing checkpoints become unusable; stop the run before discarding",
    )
    parser.add_argument(
        "--allow-dirty", action="store_true",
        help="Also approve deleting uncommitted, untracked and ignored files",
    )
    args = parser.parse_args(argv)
    print("WARNING: discarding this checkout makes its existing checkpoints unusable.", file=sys.stderr)
    result = cleanup_owned(args.repo_root, args.worktree_path, discard=True, allow_dirty=args.allow_dirty)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
