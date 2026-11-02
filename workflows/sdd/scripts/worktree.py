#!/usr/bin/env python3
"""Resolve the git worktree the implement workflow runs in.

Usage:
    worktree.py <plan> [<branch>]

Run from the directory the workflow was launched in. Always prints one JSON
object and exits 0; the workflow routes on `ok`.

- Launched inside a linked worktree: use that worktree as-is.
- Otherwise use the worktree for the plan's branch, creating it from a freshly
  fetched `origin/<default branch>` when it does not exist yet. Re-running the
  same plan finds the same branch and worktree, so a partly done plan picks up
  where it stopped.

The branch defaults to `feature/<plan file stem>`, or `feature/plan-<hash>` when
the plan is passed inline. Worktrees live in `<repo>.worktrees/` beside the
repository, the same layout the `worktree` skill uses.

When the plan is a file in the launching checkout and the worktree has no copy
at the same relative path (an uncommitted or unpushed plan), it is copied in so
the workflow can update and commit it on the branch. An existing copy in the
worktree always wins: it is either committed on the base branch or carries a
previous run's progress.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys


def git(*args: str) -> tuple[int, str, str]:
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def emit(payload: dict) -> None:
    print(json.dumps(payload))
    sys.exit(0)


def fail(error: str) -> None:
    emit({"ok": False, "error": error})


def plan_file(plan: str) -> str | None:
    """Real path when `plan` names an existing file, else None (inline content)."""
    if "\n" in plan:
        return None
    try:
        path = os.path.realpath(os.path.expanduser(plan.strip()))
        return path if os.path.isfile(path) else None
    except (OSError, ValueError):
        return None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")


def default_branch_name(plan: str, path: str | None) -> str:
    if path:
        stem = re.sub(r"\.(md|markdown|txt)$", "", os.path.basename(path), flags=re.IGNORECASE)
        stem = re.sub(r"\.plan$", "", stem, flags=re.IGNORECASE)
        if slug(stem):
            return "feature/" + slug(stem)
    return "feature/plan-" + hashlib.sha256(plan.encode()).hexdigest()[:8]


def default_branch() -> str:
    rc, ref, _ = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and ref.startswith("origin/"):
        return ref.split("/", 1)[1]
    rc, out, _ = git("ls-remote", "--symref", "origin", "HEAD")
    match = re.search(r"^ref: refs/heads/(\S+)\s+HEAD", out, re.MULTILINE) if rc == 0 else None
    return match.group(1) if match else ""


def worktree_for(branch: str) -> str | None:
    """Path of the existing worktree that has `branch` checked out, if any."""
    rc, out, _ = git("worktree", "list", "--porcelain")
    path = None
    for line in out.splitlines() if rc == 0 else []:
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == "branch refs/heads/" + branch and path and os.path.isdir(path):
            return path
    return None


def resolve_plan(plan: str, path: str | None, source_root: str, worktree: str) -> tuple[str, bool, str]:
    """Return (plan reference for the agents, whether it was copied, note)."""
    if not path:
        return plan, False, ""
    if path.startswith(worktree + os.sep):
        return path, False, ""
    rel = os.path.relpath(path, source_root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return path, False, "The plan is outside the repository, so it is edited in place and not committed."
    target = os.path.join(worktree, rel)
    if os.path.isfile(target):
        note = ""
        if os.path.realpath(target) != path:
            with open(path, "rb") as a, open(target, "rb") as b:
                if a.read() != b.read():
                    note = "The worktree's copy of the plan differs from " + path + "; using the worktree's copy."
        return target, False, note
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(path, target)
    return target, True, ""


def main() -> None:
    plan = sys.argv[1] if len(sys.argv) > 1 else ""
    requested = sys.argv[2].strip() if len(sys.argv) > 2 else ""
    if not plan.strip():
        fail("No plan was given.")

    rc, launch_root, err = git("rev-parse", "--show-toplevel")
    if rc != 0 or not launch_root:
        fail("Not inside a git repository (cwd " + os.getcwd() + "). " + err)
    launch_root = os.path.realpath(launch_root)
    path = plan_file(plan)

    rc, out, err = git("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    if rc != 0:
        fail("Could not inspect the repository: " + err)
    git_dir, common_dir = (os.path.realpath(p) for p in out.splitlines()[:2])

    if git_dir != common_dir:
        _, branch, _ = git("rev-parse", "--abbrev-ref", "HEAD")
        resolved, copied, note = resolve_plan(plan, path, launch_root, launch_root)
        emit({
            "ok": True, "error": "", "worktree_path": launch_root, "branch": branch,
            "base": "", "mode": "launched-in-worktree", "plan": resolved,
            "plan_copied": copied, "note": note,
        })

    branch = requested or default_branch_name(plan, path)
    rc, _, err = git("check-ref-format", "--branch", branch)
    if rc != 0:
        fail("Invalid branch name '" + branch + "'. " + err)

    notes = []
    worktree = worktree_for(branch)
    mode, base = "reused", ""
    if not worktree:
        base_branch = default_branch()
        if not base_branch:
            fail("Could not determine the default branch of remote 'origin'.")
        rc, _, err = git("fetch", "origin", base_branch)
        if rc != 0:
            if git("rev-parse", "--verify", "--quiet", "origin/" + base_branch)[0] != 0:
                fail("'git fetch origin " + base_branch + "' failed and origin/" + base_branch
                     + " does not exist locally. " + err)
            notes.append("Fetching origin/" + base_branch + " failed, so the last fetched copy was used.")

        repo_root = os.path.dirname(common_dir)
        worktree = os.path.join(
            os.path.dirname(repo_root),
            os.path.basename(repo_root) + ".worktrees",
            branch.split("/", 1)[-1].replace("/", "-"),
        )
        if os.path.exists(worktree):
            fail("Worktree path " + worktree + " already exists but does not have branch '" + branch
                 + "' checked out. Move it aside or pass a different `branch`.")
        os.makedirs(os.path.dirname(worktree), exist_ok=True)
        git("worktree", "prune")
        if git("rev-parse", "--verify", "--quiet", "refs/heads/" + branch)[0] == 0:
            rc, _, err = git("worktree", "add", worktree, branch)
            mode = "attached-existing-branch"
        else:
            base = "origin/" + base_branch
            rc, _, err = git("worktree", "add", "--no-track", "-b", branch, worktree, base)
            mode = "created"
        if rc != 0:
            fail("'git worktree add' failed for branch '" + branch + "'. " + err)

    worktree = os.path.realpath(worktree)
    resolved, copied, note = resolve_plan(plan, path, launch_root, worktree)
    if note:
        notes.append(note)
    emit({
        "ok": True, "error": "", "worktree_path": worktree, "branch": branch, "base": base,
        "mode": mode, "plan": resolved, "plan_copied": copied, "note": " ".join(notes),
    })


if __name__ == "__main__":
    main()
