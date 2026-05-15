#!/usr/bin/env python3
"""post_review.py — Post a rendered review + inline comments back to a GitHub PR.

Emits a JSON object on stdout that conductor auto-merges into the script
agent's output.* fields (notably `posted_comment_url`).

Usage:
    post_review.py <owner> <repo> <pr_number> <review_md_path> [inline_comments_json_path]

Behavior:
    1. Posts the review markdown as a top-level PR comment via `gh pr comment`.
    2. If inline_comments_json is provided AND non-empty AND the file exists,
       builds a single review payload from the array and posts it via
       `gh api repos/{owner}/{repo}/pulls/{n}/reviews`.

Notes:
    - GitHub's review-comment API requires a commit SHA. We resolve the head
      SHA from `gh pr view` to associate the review correctly.
    - Inline comments use `event: COMMENT` (no approval, no request-changes).
    - Network failures while posting individual inline comments are reported
      but do not abort the whole script — the top-level comment is the
      authoritative artefact.

Exit codes:
    0  success
    1  missing required positional arguments
    2  gh CLI not on PATH
    3  gh CLI not authenticated
    4  review markdown not found
    5  gh pr comment did not return a URL
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def err(payload: dict[str, object]) -> None:
    print(json.dumps(payload), file=sys.stderr)


def run_gh(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - gh is a trusted CLI
        ["gh", *args], text=True, **kwargs
    )


def build_review_payload(
    inline_path: Path, head_sha: str
) -> dict[str, Any]:
    """Read an inline-comments JSON file and produce a GitHub review payload.

    Returns one of:
      {"empty": True}                 — file present but no postable comments
      {"error": "..."}                — read or parse failure
      {"commit_id": ..., "comments": [...]}  — ready-to-POST review payload
    """
    try:
        with inline_path.open(encoding="utf-8") as f:
            inline = json.load(f)
    except Exception as e:
        return {"error": f"failed to read inline comments: {e}"}
    if not isinstance(inline, list) or not inline:
        return {"empty": True}
    review_comments: list[dict[str, Any]] = []
    for entry in inline:
        if not entry.get("file") or not entry.get("line"):
            continue
        review_comments.append({
            "path": entry["file"],
            "line": int(entry["line"]),
            "side": "RIGHT",
            "body": entry.get("body", ""),
        })
    if not review_comments:
        return {"empty": True}
    return {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": "Multi-agent PR review — inline findings.",
        "comments": review_comments,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("owner", nargs="?", default="")
    parser.add_argument("repo", nargs="?", default="")
    parser.add_argument("pr_number", nargs="?", default="")
    parser.add_argument("review_md_path", nargs="?", default="")
    parser.add_argument("inline_json_path", nargs="?", default="")
    args = parser.parse_args(argv)

    if not (args.owner and args.repo and args.pr_number and args.review_md_path):
        err({
            "error": "post_review.py requires owner, repo, pr_number, review_md_path"
        })
        return 1

    if not shutil.which("gh"):
        err({"error": "gh CLI not found on PATH."})
        return 2

    auth_check = run_gh(["auth", "status"], capture_output=True)
    if auth_check.returncode != 0:
        err({"error": "gh CLI is not authenticated. Run gh auth login."})
        return 3

    review_md_path = Path(args.review_md_path)
    if not review_md_path.is_file():
        err({"error": f"Review markdown not found at {review_md_path}"})
        return 4

    repo_slug = f"{args.owner}/{args.repo}"

    # 1. Post the top-level PR comment.
    comment_proc = run_gh(
        [
            "pr", "comment", args.pr_number,
            "--repo", repo_slug,
            "--body-file", str(review_md_path),
        ],
        capture_output=True,
    )
    # gh pr comment prints the URL on stdout (last line) on success.
    top_comment_url = ""
    if comment_proc.returncode == 0:
        lines = [ln for ln in comment_proc.stdout.splitlines() if ln.strip()]
        if lines:
            top_comment_url = lines[-1].strip()
    if not top_comment_url:
        err({
            "error": "gh pr comment did not return a URL",
            "stderr": comment_proc.stderr.strip(),
        })
        return 5

    # 2. Post inline comments if a non-empty JSON file is provided.
    inline_posted = 0
    inline_failed = 0
    inline_review_url = ""

    inline_json_path = Path(args.inline_json_path) if args.inline_json_path else None
    if inline_json_path and inline_json_path.is_file():
        # Resolve the head commit SHA for this PR.
        head_proc = run_gh(
            [
                "pr", "view", args.pr_number,
                "--repo", repo_slug,
                "--json", "headRefOid",
                "-q", ".headRefOid",
            ],
            capture_output=True,
        )
        head_sha = head_proc.stdout.strip() if head_proc.returncode == 0 else ""

        if not head_sha:
            err({"warning": "Could not resolve head SHA; skipping inline comments."})
        else:
            payload = build_review_payload(inline_json_path, head_sha)
            if payload.get("error") or payload.get("empty"):
                # Nothing to post (empty array or read error already noted).
                pass
            else:
                payload_json = json.dumps(payload)
                review_proc = run_gh(
                    [
                        "api",
                        "--method", "POST",
                        f"repos/{args.owner}/{args.repo}/pulls/{args.pr_number}/reviews",
                        "--input", "-",
                    ],
                    input=payload_json,
                    capture_output=True,
                )
                count = len(payload.get("comments", []))
                if review_proc.returncode == 0:
                    try:
                        resp = json.loads(review_proc.stdout)
                        inline_review_url = resp.get("html_url", "")
                    except json.JSONDecodeError:
                        inline_review_url = ""
                if inline_review_url:
                    inline_posted = count
                else:
                    inline_failed = count

    # Emit a JSON object on stdout — conductor auto-merges into output.*
    print(json.dumps({
        "posted_comment_url": top_comment_url,
        "posted_inline_review_url": inline_review_url,
        "inline_comments_posted": inline_posted,
        "inline_comments_failed": inline_failed,
        "poster_status": "success",
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
