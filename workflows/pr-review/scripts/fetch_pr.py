#!/usr/bin/env python3
"""fetch_pr.py — Fetch GitHub PR data using the gh CLI.

Writes metadata.json, diff.patch, and changed-files.json to a directory.
Emits a JSON object on stdout that conductor auto-merges into the script
agent's output.* fields.

Usage:
    fetch_pr.py <pr_url> [output_dir_override]

Arguments:
    pr_url               — https://github.com/<owner>/<repo>/pull/<number>
    output_dir_override  — optional; if empty, a fresh tempdir is used.

Exit codes:
    0  success
    1  missing pr_url
    2  gh CLI not on PATH
    3  gh CLI not authenticated
    4  invalid PR URL
    5  gh subprocess failed
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PR_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$"
)


def err(payload: dict[str, object]) -> None:
    print(json.dumps(payload), file=sys.stderr)


def run_gh(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Run a `gh` subprocess, returning the completed process.

    Always uses text mode (str stdin/stdout/stderr). Caller decides whether to
    check returncode or capture stdout.
    """
    return subprocess.run(  # noqa: S603 - gh is a trusted CLI, args are constructed locally
        ["gh", *args], text=True, **kwargs
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("pr_url", nargs="?", default="")
    parser.add_argument("output_dir_override", nargs="?", default="")
    args = parser.parse_args(argv)

    pr_url = args.pr_url
    if not pr_url:
        err({"error": "pr_url argument is required"})
        return 1

    if not shutil.which("gh"):
        err({
            "error": "gh CLI not found on PATH. Install from "
            "https://cli.github.com/ and run `gh auth login`."
        })
        return 2

    auth_check = run_gh(["auth", "status"], capture_output=True)
    if auth_check.returncode != 0:
        err({"error": "gh CLI is not authenticated. Run `gh auth login` first."})
        return 3

    m = PR_URL_RE.match(pr_url)
    if not m:
        err({
            "error": (
                f"Invalid GitHub PR URL: {pr_url}. "
                "Expected: https://github.com/owner/repo/pull/123"
            )
        })
        return 4
    owner, repo, number = m.group(1), m.group(2), m.group(3)

    if args.output_dir_override:
        pr_data_dir = Path(args.output_dir_override)
    else:
        pr_data_dir = Path(tempfile.mkdtemp(prefix="pr-review-"))
    pr_data_dir.mkdir(parents=True, exist_ok=True)

    metadata_raw_path = pr_data_dir / "metadata-raw.json"
    metadata_path = pr_data_dir / "metadata.json"
    diff_path = pr_data_dir / "diff.patch"
    changed_files_path = pr_data_dir / "changed-files.json"

    # 1. Fetch metadata (single JSON blob).
    fields = (
        "number,title,body,author,baseRefName,headRefName,baseRefOid,headRefOid,"
        "url,state,isDraft,labels,additions,deletions,changedFiles"
    )
    meta_proc = run_gh(
        [
            "pr", "view", number,
            "--repo", f"{owner}/{repo}",
            "--json", fields,
        ],
        capture_output=True,
    )
    if meta_proc.returncode != 0:
        err({"error": f"gh pr view failed: {meta_proc.stderr.strip()}"})
        return 5
    metadata_raw_path.write_text(meta_proc.stdout, encoding="utf-8")

    # Augment metadata with platform marker + owner/repo for downstream agents.
    meta = json.loads(meta_proc.stdout)
    meta["platform"] = "github"
    meta["owner"] = owner
    meta["repo"] = repo
    if isinstance(meta.get("author"), dict):
        meta["author_login"] = meta["author"].get("login", "")
    with metadata_path.open("w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    # 2. Fetch unified diff.
    diff_proc = run_gh(
        ["pr", "diff", number, "--repo", f"{owner}/{repo}"],
        capture_output=True,
    )
    if diff_proc.returncode != 0:
        err({"error": f"gh pr diff failed: {diff_proc.stderr.strip()}"})
        return 5
    diff_path.write_text(diff_proc.stdout, encoding="utf-8")

    # 3. Fetch the per-file changed files list (filename, status, additions,
    #    deletions, patch).
    files_proc = run_gh(
        [
            "api",
            f"repos/{owner}/{repo}/pulls/{number}/files",
            "--paginate",
        ],
        capture_output=True,
    )
    if files_proc.returncode != 0:
        err({"error": f"gh api files failed: {files_proc.stderr.strip()}"})
        return 5
    changed_files_path.write_text(files_proc.stdout, encoding="utf-8")

    # Emit a JSON object on stdout. Conductor auto-merges these fields into
    # pr_fetcher.output.* for downstream agents.
    out = {
        "pr_data_dir": str(pr_data_dir),
        "pr_url": pr_url,
        "owner": owner,
        "repo": repo,
        "pr_number": int(number),
        "pr_title": meta.get("title", ""),
        "platform": "github",
        "fetcher_status": "success",
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
