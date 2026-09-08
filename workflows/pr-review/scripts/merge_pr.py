#!/usr/bin/env python3
"""Merge the reviewed head after a separate human decision and fresh readiness check.

Usage:
    merge_pr.py <name_with_owner> <pr_number> <method> <auto> <delete_branch> <head_sha>
"""

from __future__ import annotations

import json
import subprocess
import sys

from merge_readiness import inspect_readiness

METHODS = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}

# The refusal that means "this repository has not enabled auto-merge", as
# opposed to a refusal that means the merge itself is not allowed.
AUTO_UNAVAILABLE = (
    "auto-merge is not allowed",
    "auto merge is not allowed",
    "auto-merge is not enabled",
    "auto merge is not enabled",
    "does not have auto-merge enabled",
    "allow_auto_merge",
)


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))
    sys.exit(0)


def run(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(args, capture_output=True, text=True)  # noqa: S603
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def result(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": False,
        "error": "",
        "merged": False,
        "queued": False,
        "state": "",
        "method": "",
        "branch_deleted": False,
        "auto_used": False,
        "pr_url": "",
    }
    payload.update(overrides)
    return payload


def truthy(value: str) -> bool:
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def read_state(nwo: str, pr_number: str) -> tuple[dict[str, object] | None, str]:
    """What GitHub currently thinks of this pull request."""
    rc, out, err = run(
        [
            "gh",
            "pr",
            "view",
            pr_number,
            "-R",
            nwo,
            "--json",
            "state,url,mergedAt,autoMergeRequest,headRefName",
        ]
    )
    if rc != 0 or not out:
        return None, err or out or "'gh pr view' returned a non-zero exit code"
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        return None, f"'gh pr view' did not return JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "'gh pr view' did not return an object"
    return parsed, ""


def branch_gone(nwo: str, branch: str) -> bool:
    if not branch:
        return False
    rc, _, _ = run(["gh", "api", f"repos/{nwo}/branches/{branch}"])
    return rc != 0


def main(argv: list[str]) -> None:
    if len(argv) < 6 or not argv[5].strip():
        emit(result(error="merge_pr.py requires name_with_owner, pr_number, method, auto, delete_branch and reviewed head_sha"))

    nwo, pr_number, method = argv[0], argv[1], argv[2].strip().casefold()
    auto = truthy(argv[3]) if len(argv) > 3 else False
    delete_branch = truthy(argv[4]) if len(argv) > 4 else False
    head_sha = argv[5].strip()

    if not pr_number.isdigit():
        emit(result(error=f"Pull request number is not numeric: {pr_number!r}"))
    if method not in METHODS:
        emit(result(error=f"Unknown merge method {method!r}. Use squash, merge or rebase."))

    before, error = read_state(nwo, pr_number)
    if before is None:
        emit(result(method=method, error=f"Could not read {nwo}#{pr_number}. {error}"))

    state = str(before.get("state") or "")
    pr_url = str(before.get("url") or "")
    branch = str(before.get("headRefName") or "")

    if state == "MERGED":
        emit(
            result(
                ok=True,
                merged=True,
                state=state,
                method=method,
                pr_url=pr_url,
                error="It was already merged before this step ran, so nothing was done.",
            )
        )
    if state != "OPEN":
        emit(
            result(
                state=state,
                method=method,
                pr_url=pr_url,
                error=f"{nwo}#{pr_number} is {state.lower()}, so there is nothing to merge.",
            )
        )

    readiness = inspect_readiness(nwo, pr_number, head_sha)
    if not readiness["ok"] or not readiness["can_merge"]:
        details = "; ".join(str(issue) for issue in readiness["issues"])
        emit(
            result(
                state=state,
                method=method,
                pr_url=pr_url,
                error=f"Readiness changed or could not be confirmed. {details} {readiness['error']}".strip(),
            )
        )

    base = [
        "gh", "pr", "merge", pr_number, "-R", nwo, METHODS[method],
        "--match-head-commit", head_sha,
    ]
    if delete_branch:
        base.append("--delete-branch")

    auto_used = auto
    rc, out, err = run([*base, "--auto"] if auto else base)
    if rc != 0 and auto:
        haystack = f"{err}\n{out}".casefold()
        if any(marker in haystack for marker in AUTO_UNAVAILABLE):
            auto_used = False
            rc, out, err = run(base)

    # The command's exit code is not trusted: `--delete-branch` fails on its
    # own well after the merge landed. GitHub's own view of the pull request
    # is the only thing that settles it.
    after, state_error = read_state(nwo, pr_number)
    if after is None:
        emit(
            result(
                method=method,
                auto_used=auto_used,
                pr_url=pr_url,
                error=(
                    f"The merge command {'failed' if rc != 0 else 'ran'} and the "
                    f"result could not be confirmed, so nothing here says whether "
                    f"#{pr_number} merged. Check it by hand. {err or state_error}"
                ),
            )
        )

    state = str(after.get("state") or "")
    merged = state == "MERGED" or bool(after.get("mergedAt"))
    queued = not merged and bool(after.get("autoMergeRequest"))

    if not merged and not queued:
        emit(
            result(
                state=state,
                method=method,
                auto_used=auto_used,
                pr_url=pr_url,
                error=err or out or f"'gh pr merge' left #{pr_number} {state.lower()}.",
            )
        )

    emit(
        result(
            ok=True,
            merged=merged,
            queued=queued,
            state=state,
            method=method,
            auto_used=auto_used,
            pr_url=pr_url,
            branch_deleted=delete_branch and merged and branch_gone(nwo, branch),
            # A merge that landed with a branch left behind is still a merge,
            # so the tidy-up failing is reported rather than raised.
            error="" if rc == 0 else (err or out),
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
