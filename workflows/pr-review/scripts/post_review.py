#!/usr/bin/env python3
"""Post one review to a pull request, anchoring inline comments where it can.

GitHub rejects an entire review with a 422 if any one comment names a line
outside the diff, so a single bad anchor would lose every comment. Rather than
gamble, this parses the diff, keeps the comments whose `(path, line)` lands on
the right-hand side of a hunk, and folds the rest into the review body.

The diff is read from the worktree the review actually ran against, so the
anchors match the code that was read rather than whatever the branch looks like
by the time this runs. `gh pr diff` is the fallback.

If the POST is refused outright it retries once with every comment folded into
the body. A review that reads slightly worse beats a review that was never
posted. It never retries a failure that might have been accepted, such as a
timeout, because that would post a second public review.

Usage:
    post_review.py <name_with_owner> <pr_number> <head_sha> <worktree_path> <base_ref> <remote> [event]

    stdin: {"body": "...", "comments": [{"path": "...", "line": 1, "body": "..."}]}

Output:
    ok, error, review_url, inline_posted, inline_demoted, fallback_used
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

DEMOTED_HEADING = "### Findings that could not be anchored inline"
DEMOTED_NOTE = (
    "These name a line outside this pull request's diff, so GitHub cannot "
    "attach them to a specific line."
)
EMPTY_BODY_FALLBACK = "Review findings."


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))
    sys.exit(0)


def run(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(args, input=stdin, capture_output=True, text=True)  # noqa: S603
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def fail(message: str) -> None:
    emit(
        {
            "ok": False,
            "error": message,
            "review_url": "",
            "inline_posted": 0,
            "inline_demoted": 0,
            "fallback_used": False,
        }
    )


def diff_target_path(header: str) -> str:
    """Extract the new-file path from a `+++` line, or empty for a deletion."""
    path = header[4:].strip()
    if path == "/dev/null":
        return ""
    if path.startswith(("b/", "a/")):
        path = path[2:]
    return path


def anchorable_lines(diff: str) -> dict[str, set[int]]:
    """Map each path to the new-side line numbers a RIGHT comment may target.

    Added and context lines both qualify; removed lines exist only on the left.

    `+++` is only read as a file header while one is expected, because an added
    source line beginning with `++` appears in a unified diff as `+++ ...` and
    would otherwise reset the path mid-hunk, losing every later anchor in the
    file.
    """
    lines: dict[str, set[int]] = {}
    path = ""
    cursor = 0
    in_hunk = False
    expect_header = False

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path, cursor, in_hunk, expect_header = "", 0, False, True
            continue
        if raw.startswith("--- "):
            expect_header = True
            continue
        if expect_header and raw.startswith("+++ "):
            path = diff_target_path(raw)
            in_hunk = False
            expect_header = False
            continue
        match = HUNK.match(raw)
        if match:
            cursor = int(match.group(1))
            in_hunk = bool(path)
            expect_header = False
            continue
        if not in_hunk or not path:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("+") or raw.startswith(" ") or raw == "":
            lines.setdefault(path, set()).add(cursor)
            cursor += 1
        elif raw.startswith("-"):
            continue
        else:
            in_hunk = False

    return lines


def load_diff(worktree: str, base_ref: str, remote: str, nwo: str, pr_number: str) -> str:
    """Read the PR diff locally, falling back to GitHub's own view of it."""
    if worktree and base_ref:
        for base in (f"{remote}/{base_ref}", base_ref):
            rc, out, _ = run(["git", "-C", worktree, "diff", "--no-color", f"{base}...HEAD"])
            if rc == 0 and out:
                return out
    rc, out, _ = run(["gh", "pr", "diff", pr_number, "--repo", nwo])
    return out if rc == 0 else ""


def demote(body: str, demoted: list[dict[str, object]]) -> str:
    """Append the unanchorable comments to the review body."""
    if not demoted:
        return body
    parts = [body.strip(), "", DEMOTED_HEADING, "", DEMOTED_NOTE, ""]
    for comment in demoted:
        location = str(comment.get("path") or "").strip()
        line = comment.get("line")
        if location and line:
            location = f"{location}:{line}"
        heading = f"**{location}**" if location else "**General**"
        parts.append(f"{heading}\n\n{str(comment.get('body') or '').strip()}\n")
    return "\n".join(parts).strip()


def post(nwo: str, pr_number: str, payload: dict[str, object]) -> tuple[str, str]:
    """POST the review, returning (html_url, error)."""
    rc, out, err = run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{nwo}/pulls/{pr_number}/reviews",
            "--input",
            "-",
        ],
        stdin=json.dumps(payload),
    )
    if rc != 0:
        return "", (err or out or "gh api returned a non-zero exit code")
    try:
        return json.loads(out).get("html_url", ""), ""
    except json.JSONDecodeError:
        return "", f"Could not parse the GitHub response: {out[:400]}"


def rejected_outright(error: str) -> bool:
    """Whether GitHub refused the payload rather than possibly accepting it.

    Only a validation refusal is safe to retry. A timeout or dropped response
    may have been accepted, and retrying that posts a second public review.
    """
    lowered = error.lower()
    return "422" in lowered or "unprocessable" in lowered


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        fail("post_review.py requires name_with_owner, pr_number and head_sha")

    nwo, pr_number, head_sha = argv[0], argv[1], argv[2]
    worktree = argv[3] if len(argv) > 3 else ""
    base_ref = argv[4] if len(argv) > 4 else ""
    remote = argv[5] if len(argv) > 5 else "origin"
    event = (argv[6] if len(argv) > 6 else "COMMENT") or "COMMENT"

    raw = sys.stdin.read().strip()
    if not raw:
        fail("No review payload was provided on stdin")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"Review payload is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        fail(f"Review payload must be a JSON object, got {type(parsed).__name__}")

    body = str(parsed.get("body") or "").strip()
    original_body = body
    requested = parsed.get("comments") or []
    if not isinstance(requested, list):
        requested = []

    anchors = anchorable_lines(
        load_diff(worktree, base_ref, remote, nwo, pr_number)
    )

    inline: list[dict[str, object]] = []
    demoted: list[dict[str, object]] = []
    for comment in requested:
        if not isinstance(comment, dict):
            continue
        text = str(comment.get("body") or "").strip()
        if not text:
            continue
        path = str(comment.get("path") or comment.get("file") or "").strip()
        try:
            line = int(str(comment.get("line")).strip())
        except (TypeError, ValueError):
            line = 0
        if path and line > 0 and line in anchors.get(path, set()):
            inline.append({"path": path, "line": line, "side": "RIGHT", "body": text})
        else:
            demoted.append({"path": path, "line": line, "body": text})

    body = demote(original_body, demoted)
    if not body:
        if not inline:
            fail("The review has no body and no postable comments")
        body = EMPTY_BODY_FALLBACK

    payload: dict[str, object] = {"commit_id": head_sha, "event": event, "body": body}
    if inline:
        payload["comments"] = inline

    url, error = post(nwo, pr_number, payload)
    if url:
        emit(
            {
                "ok": True,
                "error": "",
                "review_url": url,
                "inline_posted": len(inline),
                "inline_demoted": len(demoted),
                "fallback_used": False,
            }
        )

    if not inline or not rejected_outright(error):
        fail(f"Could not post the review: {error}")

    # GitHub refused the payload outright, so nothing was created and the only
    # thing it can have objected to is an anchor. Rebuild from the original
    # body rather than the already-demoted one, which would repeat every
    # finding that was folded in the first time.
    retry_body = demote(original_body, demoted + inline) or EMPTY_BODY_FALLBACK
    retry_url, retry_error = post(
        nwo, pr_number, {"commit_id": head_sha, "event": event, "body": retry_body}
    )
    if not retry_url:
        fail(f"Could not post the review: {error}. Retry without inline comments: {retry_error}")

    emit(
        {
            "ok": True,
            "error": f"Inline comments were rejected and folded into the body: {error}",
            "review_url": retry_url,
            "inline_posted": 0,
            "inline_demoted": len(demoted) + len(inline),
            "fallback_used": True,
        }
    )


if __name__ == "__main__":
    main(sys.argv[1:])
