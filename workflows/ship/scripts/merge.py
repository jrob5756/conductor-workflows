#!/usr/bin/env python3
"""Squash-merge the shipped pull request and clean up its branch and worktree.

Usage:
    merge.py <name_with_owner> <pr_number> <branch> <worktree_path> <repo_root>
             <default_branch> <issue_number> <gh_user>

Always prints one JSON object and exits 0; the workflow routes on its fields.
`merged` and `blocked_by_conflicts` are never both true, and a run that neither
merged nor hit conflicts carries the reason in `error` so the workflow can fail
loudly instead of ending as if it had shipped.

This replaced an agent that drove the `merge-pr` skill: once that skill was
marked `disable-model-invocation`, the agent could not load it, merged nothing
and reported success. A merge needs no judgement, so it is a script.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

# A gh call that never reached GitHub, or a GitHub-side blip. Reads retry on it;
# the merge retries only after re-reading the PR, so it cannot merge twice.
TRANSIENT = re.compile(
    r"TLS handshake timeout|i/o timeout|connection (refused|reset)|no such host|"
    r"unexpected EOF|timeout awaiting response headers|\bHTTP 50[0234]\b",
    re.IGNORECASE,
)
CONFLICT = re.compile(r"merge conflict|conflicts? (with|in)|not mergeable: .*conflict", re.IGNORECASE)
BACKOFF = (2, 5, 10)
UNKNOWN_POLLS = 12
UNKNOWN_DELAY = 5


def run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=120)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"{args[0]} could not complete: {exc}"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def gh_json(args: list[str]) -> dict:
    """Run a read-only gh command, retrying transient failures."""
    for delay in (*BACKOFF, None):
        rc, out, err = run(["gh", *args])
        if rc == 0:
            try:
                return json.loads(out)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"gh {' '.join(args[:2])} returned invalid JSON: {exc}") from exc
        if delay is None or not TRANSIENT.search(err):
            raise RuntimeError(f"gh {' '.join(args[:2])}: {err or out or 'failed'}")
        time.sleep(delay)
    raise AssertionError("unreachable")


def result(**overrides: object) -> dict:
    payload = {
        "ok": False,
        "error": "",
        "merged": False,
        "blocked_by_conflicts": False,
        "conflict_summary": "",
        "merge_commit": "",
        "branch_deleted": False,
        "worktree_removed": False,
        "issue_closed": False,
        "cleanup_notes": "",
    }
    payload.update(overrides)
    return payload


PR_FIELDS = "state,mergeable,mergeStateStatus,headRefName,headRefOid,author,url,mergeCommit"


def read_pr(nwo: str, pr: str, *, settle: bool = True) -> dict:
    """Read the PR, waiting out GitHub's UNKNOWN mergeability while it computes."""
    for attempt in range(UNKNOWN_POLLS):
        view = gh_json(["pr", "view", pr, "-R", nwo, "--json", PR_FIELDS])
        if not settle or view.get("state") != "OPEN" or view.get("mergeable") != "UNKNOWN":
            return view
        if attempt < UNKNOWN_POLLS - 1:
            time.sleep(UNKNOWN_DELAY)
    return view


def conflict_summary(repo_root: str, base: str, branch: str) -> str:
    """Name the conflicting files without touching any working tree."""
    run(["git", "-C", repo_root, "fetch", "--quiet", "origin", base, branch])
    rc, out, _ = run([
        "git", "-C", repo_root, "merge-tree", "--write-tree", "--name-only", "--no-messages",
        f"origin/{base}", f"origin/{branch}",
    ])
    files = [line for line in out.splitlines()[1:] if line.strip()] if rc == 1 else []
    if files:
        return f"`{branch}` conflicts with `{base}` in: " + ", ".join(files)
    return f"GitHub reports `{branch}` conflicts with `{base}`; the conflicting files could not be listed locally."


def merge(nwo: str, pr: str) -> tuple[dict, str]:
    """Squash-merge, re-reading the PR before every retry. Returns (view, error)."""
    for delay in (*BACKOFF, None):
        rc, out, err = run(["gh", "pr", "merge", pr, "-R", nwo, "--squash"])
        view = read_pr(nwo, pr, settle=False)
        if view.get("state") == "MERGED":
            return view, ""
        if rc == 0:
            return view, "gh reported success but the pull request is not merged."
        if delay is None or not TRANSIENT.search(err):
            return view, err or out or "gh pr merge failed."
        time.sleep(delay)
    raise AssertionError("unreachable")


def remove_worktree(repo_root: str, worktree: str, notes: list[str]) -> bool:
    if not worktree or not os.path.isdir(worktree):
        run(["git", "-C", repo_root, "worktree", "prune"])
        return True
    rc, dirty, err = run(["git", "-C", worktree, "status", "--porcelain"])
    if rc != 0:
        notes.append(f"Worktree {worktree} kept: could not read its status ({err}).")
        return False
    if dirty:
        notes.append(f"Worktree {worktree} kept: it has uncommitted or untracked changes.")
        return False
    rc, _, err = run(["git", "-C", repo_root, "worktree", "remove", worktree])
    run(["git", "-C", repo_root, "worktree", "prune"])
    if rc != 0:
        notes.append(f"Worktree {worktree} kept: {err}")
        return False
    return True


def delete_branches(repo_root: str, branch: str, head_sha: str, notes: list[str]) -> bool:
    local_gone = True
    rc, local_sha, _ = run(["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    if rc == 0 and local_sha:
        # A squash merge leaves the branch "unmerged" to git, so -d always refuses.
        # Force-delete only when every local commit reached the merged PR head.
        run(["git", "-C", repo_root, "fetch", "--quiet", "origin", head_sha])
        rc, _, _ = run(["git", "-C", repo_root, "merge-base", "--is-ancestor", local_sha, head_sha])
        if rc == 0:
            rc, _, err = run(["git", "-C", repo_root, "branch", "-D", branch])
            local_gone = rc == 0
            if not local_gone:
                notes.append(f"Local branch {branch} kept: {err}")
        else:
            local_gone = False
            notes.append(f"Local branch {branch} kept: it has commits that are not in the merged pull request.")
    remote_gone = True
    rc, listed, err = run(["git", "-C", repo_root, "ls-remote", "--heads", "origin", branch])
    if rc != 0:
        remote_gone = False
        notes.append(f"Remote branch {branch} not checked: {err}")
    elif listed:
        rc, _, err = run(["git", "-C", repo_root, "push", "--quiet", "origin", "--delete", branch])
        remote_gone = rc == 0
        if not remote_gone:
            notes.append(f"Remote branch {branch} kept: {err}")
    return local_gone and remote_gone


def update_base(repo_root: str, base: str, notes: list[str]) -> None:
    rc, current, _ = run(["git", "-C", repo_root, "branch", "--show-current"])
    rc_status, dirty, _ = run(["git", "-C", repo_root, "status", "--porcelain", "--untracked-files=no"])
    if rc == 0 and current == base and rc_status == 0 and not dirty:
        rc, _, err = run(["git", "-C", repo_root, "pull", "--quiet", "--ff-only", "origin", base])
        if rc != 0:
            notes.append(f"Could not fast-forward {base} in {repo_root}: {err}")
    else:
        run(["git", "-C", repo_root, "fetch", "--quiet", "origin", base])
        notes.append(f"{repo_root} is not on a clean {base}; fetched origin/{base} without updating the checkout.")


def issue_closed(nwo: str, issue: str) -> bool:
    if not issue.isdigit() or int(issue) < 1:
        return False
    for attempt in range(4):
        try:
            if gh_json(["issue", "view", issue, "-R", nwo, "--json", "state"]).get("state") == "CLOSED":
                return True
        except RuntimeError:
            return False
        if attempt < 3:
            time.sleep(3)
    return False


def main(argv: list[str]) -> dict:
    if len(argv) != 8:
        return result(error="Usage: merge.py <nwo> <pr> <branch> <worktree> <repo_root> <base> <issue> <gh_user>")
    nwo, pr, branch, worktree, repo_root, base, issue, gh_user = argv
    try:
        view = read_pr(nwo, pr)
    except RuntimeError as exc:
        return result(error=str(exc))
    if view.get("headRefName") != branch:
        return result(error=f"PR #{pr} is on `{view.get('headRefName')}`, not the shipped branch `{branch}`.")
    state = view.get("state")
    if state == "CLOSED":
        return result(error=f"PR #{pr} was closed without merging.")
    notes: list[str] = []
    if state == "MERGED":
        notes.append(f"PR #{pr} was already merged before this step ran.")
    elif view.get("mergeable") == "CONFLICTING":
        return result(ok=True, blocked_by_conflicts=True, conflict_summary=conflict_summary(repo_root, base, branch))
    else:
        try:
            view, error = merge(nwo, pr)
        except RuntimeError as exc:
            return result(error=f"Could not confirm the merge of PR #{pr}: {exc}")
        if error:
            if view.get("mergeable") == "CONFLICTING" or CONFLICT.search(error):
                return result(ok=True, blocked_by_conflicts=True, conflict_summary=conflict_summary(repo_root, base, branch))
            return result(error=f"PR #{pr} was not merged: {error}")

    payload = result(ok=True, merged=True, merge_commit=(view.get("mergeCommit") or {}).get("oid", ""))
    author = ((view.get("author") or {}).get("login") or "").casefold()
    if author and author != gh_user.casefold():
        notes.append(f"PR #{pr} is authored by {author}, not {gh_user}; its branch and worktree were left for the author.")
    else:
        payload["worktree_removed"] = remove_worktree(repo_root, worktree, notes)
        if payload["worktree_removed"]:
            payload["branch_deleted"] = delete_branches(repo_root, branch, view.get("headRefOid", ""), notes)
        else:
            notes.append(f"Branch {branch} kept because its worktree is still in place.")
        update_base(repo_root, base, notes)
    payload["issue_closed"] = issue_closed(nwo, issue)
    payload["cleanup_notes"] = " ".join(notes)
    return payload


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1:])))
