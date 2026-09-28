#!/usr/bin/env python3
"""Validate approved finding coverage and publish against a freshly checked PR head.

Findings use {mode, approved, items}; concept/note/approval use {mode, body}.
Every POST, including a validation retry, checks the current head and open state.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid

from review_format import render

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

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
            "body_count": 0,
            "posted_count": 0,
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


def fresh_pr(nwo: str, pr_number: str, head_sha: str) -> str:
    rc, out, err = run(["gh", "api", f"repos/{nwo}/pulls/{pr_number}"])
    if rc:
        return f"Could not verify the current PR before publication: {err or out}"
    try:
        pr = json.loads(out)
    except json.JSONDecodeError as exc:
        return f"Could not parse the current PR before publication: {exc}"
    if not isinstance(pr, dict) or not isinstance(pr.get("head"), dict):
        return "Current PR response has no valid head."
    if pr.get("state") != "open" or pr.get("merged") is not False:
        return "The PR is closed, merged, or its open state could not be verified. Nothing was posted."
    if pr["head"].get("sha") != head_sha:
        return "The PR head changed since review. Start a new review; nothing was posted."
    return ""


def post(nwo: str, pr_number: str, payload: dict[str, object]) -> tuple[str, str, bool]:
    """Return (URL, error, safe-to-retry); freshness failures never permit a POST."""
    error = fresh_pr(nwo, pr_number, str(payload["commit_id"]))
    if error:
        return "", error, False
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
        error = err or out or "gh api returned a non-zero exit code"
        return "", error, rejected_outright(error)
    try:
        response = json.loads(out)
        url = response.get("html_url") if isinstance(response, dict) else None
        if not isinstance(url, str) or not url:
            return "", "GitHub accepted the request but returned no review URL; do not retry.", False
        return url, "", False
    except json.JSONDecodeError:
        return "", f"Could not parse the GitHub response: {out[:400]}", False


def rejected_outright(error: str) -> bool:
    """Only a definitive HTTP validation refusal is safe to retry."""
    return bool(re.search(r"\(HTTP 422\)", error))


def validate_findings(parsed):
    approved, items = parsed.get("approved"), parsed.get("items")
    if not isinstance(approved, list) or not approved or not isinstance(items, list):
        raise ValueError("Findings require a nonempty approved array and an items array.")
    by_id = {}
    for finding in approved:
        if not isinstance(finding, dict):
            raise ValueError("Malformed approved finding.")
        identity = finding.get("id")
        if not isinstance(identity, str) or not identity.strip() or identity in by_id:
            raise ValueError("Approved finding IDs must be nonempty and unique.")
        if (
            not isinstance(finding.get("body"), str) or not finding["body"].strip()
            or not isinstance(finding.get("path", ""), str)
            or type(finding.get("line", 0)) is not int or finding.get("line", 0) < 0
            or finding.get("source_type", "code") not in ("code", "ci")
        ):
            raise ValueError(f"Malformed approved finding {identity}.")
        by_id[identity] = finding
    written = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"finding_id", "body"}:
            raise ValueError("Writer items must contain only finding_id and body.")
        identity, body = item["finding_id"], item["body"]
        if not isinstance(identity, str) or identity not in by_id or identity in written:
            raise ValueError("Writer returned an unknown or duplicate finding ID.")
        if not isinstance(body, str) or not body.strip():
            raise ValueError(f"Writer returned an empty or malformed body for {identity}.")
        written[identity] = body.strip()
    if written.keys() != by_id.keys():
        raise ValueError("Writer omitted approved finding IDs: " + ", ".join(by_id.keys() - written.keys()))
    batch = uuid.uuid4().hex
    return [
        {
            "finding_id": f"{batch}:{identity}",
            "source_type": finding.get("source_type", "code"),
            "path": finding.get("path", ""),
            "line": finding.get("line", 0),
            "body": written[identity],
        }
        for identity, finding in by_id.items()
    ]


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        fail("post_review.py requires name_with_owner, pr_number and head_sha")

    nwo, pr_number, head_sha = argv[0], argv[1], argv[2]
    worktree = argv[3] if len(argv) > 3 else ""
    base_ref = argv[4] if len(argv) > 4 else ""
    remote = argv[5] if len(argv) > 5 else "origin"
    event = (argv[6] if len(argv) > 6 else "COMMENT") or "COMMENT"
    if not head_sha or not pr_number.isdigit() or event not in ("COMMENT", "APPROVE"):
        fail("A reviewed head, numeric PR number and COMMENT or APPROVE event are required.")

    raw = sys.stdin.read().strip()
    if not raw:
        fail("No review payload was provided on stdin")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"Review payload is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        fail(f"Review payload must be a JSON object, got {type(parsed).__name__}")

    mode = parsed.get("mode")
    findings = []
    inline = []
    demoted = 0
    if mode == "findings":
        if event != "COMMENT":
            fail("Finding reviews must use COMMENT.")
        opening = parsed.get("opening")
        if not isinstance(opening, str) or not opening.strip():
            fail("Finding reviews require a nonempty opening.")
        opening = opening.strip()
        try:
            findings = validate_findings(parsed)
        except ValueError as exc:
            fail(str(exc))
        anchors = anchorable_lines(load_diff(worktree, base_ref, remote, nwo, pr_number))
        for item in findings:
            path, line = item["path"], item["line"]
            item["placement"] = "inline" if path and line in anchors.get(path, set()) else "body"
            if item["placement"] == "inline":
                inline.append({"path": path, "line": line, "side": "RIGHT", "body": item["body"]})
            elif path and line:
                demoted += 1
        body = render(findings, opening)
    elif mode in ("concept", "note", "approval"):
        if (mode == "approval") != (event == "APPROVE"):
            fail("Approval mode and event must agree.")
        if set(parsed) != {"mode", "body"}:
            fail("Concept, note and approval payloads accept only mode and body.")
        body = parsed.get("body")
        if not isinstance(body, str) or not body.strip():
            fail("The review body must be a nonempty string.")
    else:
        fail("Review mode must be findings, concept, note or approval.")

    payload: dict[str, object] = {"commit_id": head_sha, "event": event, "body": body}
    if inline:
        payload["comments"] = inline

    url, error, retryable = post(nwo, pr_number, payload)
    if url:
        emit(
            {
                "ok": True,
                "error": "",
                "review_url": url,
                "inline_posted": len(inline),
                "inline_demoted": demoted,
                "body_count": len(findings) - len(inline),
                "posted_count": len(findings),
                "fallback_used": False,
            }
        )

    if not inline or not retryable:
        fail(f"Could not post the review: {error}")

    for item in findings:
        item["placement"] = "body"
    retry_url, retry_error, _ = post(
        nwo, pr_number, {"commit_id": head_sha, "event": event, "body": render(findings, opening)}
    )
    if not retry_url:
        fail(f"Could not post the review: {error}. Retry without inline comments: {retry_error}")

    emit(
        {
            "ok": True,
            "error": f"Inline comments were rejected and folded into the body: {error}",
            "review_url": retry_url,
            "inline_posted": 0,
            "inline_demoted": demoted + len(inline),
            "body_count": len(findings),
            "posted_count": len(findings),
            "fallback_used": True,
        }
    )


if __name__ == "__main__":
    main(sys.argv[1:])
