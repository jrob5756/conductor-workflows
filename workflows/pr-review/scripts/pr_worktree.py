#!/usr/bin/env python3
"""Create a uniquely owned PR checkout; interrupted reviews remain resumable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

from worktree_lifecycle import (
    LifecycleError, git, ref_sha, registry, remove_owned, runner_context, safe_path, write_record,
)


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))
    sys.exit(0)


def run(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(args, capture_output=True, text=True)  # noqa: S603
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def fail(message: str, **provenance: object) -> None:
    emit(
        {
            "ok": False,
            "error": message,
            "worktree_path": "",
            "branch": "",
            "base_ref": "",
            "remote": "",
            "head_sha": "",
            "base_sha": "",
            "ownership_id": "",
            **provenance,
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


def main(argv: list[str]) -> None:
    if len(argv) < 4:
        fail("pr_worktree.py requires repo_root, worktrees_dir, pr_number and name_with_owner")

    repo_root, worktrees_dir, pr_number, name_with_owner = argv[0], argv[1], argv[2], argv[3]
    base_ref = argv[4] if len(argv) > 4 else ""

    if not pr_number.isdigit():
        fail(f"Pull request number is not numeric: {pr_number!r}")

    ownership_id = uuid.uuid4().hex
    branch = f"pr-review/{pr_number}-{ownership_id}"
    record = None
    remote = ""
    try:
        repo_root = str(safe_path(repo_root))
        worktrees = safe_path(worktrees_dir)
        worktree_path = str(worktrees / f"pr-{pr_number}-{ownership_id}")
        if worktrees == safe_path(repo_root) or safe_path(repo_root) in worktrees.parents:
            raise LifecycleError("Review worktrees must live outside the repository checkout.")
        if base_ref:
            git(repo_root, "check-ref-format", f"refs/heads/{base_ref}")
        context = runner_context()
        remote = resolve_remote(repo_root, name_with_owner)
        with registry(repo_root) as (directory, common):
            if os.path.lexists(worktree_path) or ref_sha(repo_root, f"refs/heads/{branch}"):
                raise LifecycleError("Unique review identity already exists; nothing overwritten.")
            base_pin = f"refs/pr-review/{ownership_id}/base"
            if ref_sha(repo_root, base_pin):
                raise LifecycleError("Unique base identity already exists; nothing overwritten.")
            record = {
                "version": 1, "ownership_id": ownership_id, "repo_root": repo_root,
                "common_dir": common, "worktree_path": worktree_path, "branch": branch,
                "head_sha": "", "base_sha": "", "base_ref": base_ref, "base_pin": base_pin,
                "worktree_identity": None, **context,
            }
            write_record(directory, record, create=True)
            try:
                worktrees.mkdir(parents=True, exist_ok=True)
                git(repo_root, "fetch", "--no-write-fetch-head", "--", remote,
                    f"pull/{pr_number}/head:refs/heads/{branch}")
                record["head_sha"] = git(repo_root, "rev-parse", f"refs/heads/{branch}^{{commit}}")
                write_record(directory, record)
                if base_ref:
                    git(repo_root, "fetch", "--no-write-fetch-head", "--", remote,
                        f"refs/heads/{base_ref}:{base_pin}")
                    record["base_sha"] = git(repo_root, "rev-parse", f"{base_pin}^{{commit}}")
                    write_record(directory, record)
                git(repo_root, "worktree", "add", "--", worktree_path, branch)
                info = safe_path(worktree_path).stat()
                record["worktree_identity"] = [info.st_dev, info.st_ino]
                write_record(directory, record)
                if git(worktree_path, "rev-parse", "HEAD") != record["head_sha"]:
                    raise LifecycleError("Created checkout does not match the fetched review head.")
            except (LifecycleError, OSError) as exc:
                try:
                    remove_owned(repo_root, record)
                    (directory / f"{ownership_id}.json").unlink()
                except (LifecycleError, OSError) as cleanup_error:
                    raise LifecycleError(
                        f"{exc} Partial resources retained at {worktree_path}: {cleanup_error}"
                    ) from exc
                raise
    except (LifecycleError, OSError) as exc:
        fail(str(exc), **({
            "worktree_path": record["worktree_path"], "branch": branch,
            "ownership_id": ownership_id, "head_sha": record["head_sha"],
            "base_sha": record["base_sha"], "base_ref": base_ref,
        } if record else {}))

    emit(
        {
            "ok": True,
            "error": "",
            "worktree_path": worktree_path,
            "branch": branch,
            "base_ref": base_ref,
            "remote": remote,
            "head_sha": record["head_sha"],
            "base_sha": record["base_sha"],
            "ownership_id": ownership_id,
        }
    )


if __name__ == "__main__":
    main(sys.argv[1:])
