#!/usr/bin/env bash
#
# post-review.sh — Post a rendered review comment + inline comments back to a
# GitHub PR using the gh CLI.
#
# Emits a JSON object on stdout that conductor auto-merges into the script
# agent's output.* fields (notably `posted_comment_url`).
#
# Usage:
#   post-review.sh <owner> <repo> <pr_number> <review_md_path> [inline_comments_json_path]
#
# Behavior:
#   1. Posts the review markdown as a top-level PR comment via `gh pr comment`.
#   2. If inline_comments_json is provided AND non-empty AND the file exists,
#      iterates the array and posts each one as a single-comment review via
#      `gh api repos/{owner}/{repo}/pulls/{n}/reviews`.
#
# Notes:
#   - GitHub's review-comment API requires a commit SHA. We resolve the head
#     SHA from `gh pr view` to associate the review correctly.
#   - Inline comments use `event: COMMENT` (no approval, no request-changes).
#   - Network failures while posting individual inline comments are reported
#     but do not abort the whole script — the top-level comment is the
#     authoritative artefact.

set -euo pipefail

OWNER="${1:-}"
REPO="${2:-}"
PR_NUMBER="${3:-}"
REVIEW_MD_PATH="${4:-}"
INLINE_JSON_PATH="${5:-}"

if [[ -z "$OWNER" || -z "$REPO" || -z "$PR_NUMBER" || -z "$REVIEW_MD_PATH" ]]; then
  echo '{"error": "post-review.sh requires owner, repo, pr_number, review_md_path"}' >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo '{"error": "gh CLI not found on PATH."}' >&2
  exit 2
fi
if ! gh auth status >/dev/null 2>&1; then
  echo '{"error": "gh CLI is not authenticated. Run gh auth login."}' >&2
  exit 3
fi

if [[ ! -f "$REVIEW_MD_PATH" ]]; then
  echo "{\"error\": \"Review markdown not found at $REVIEW_MD_PATH\"}" >&2
  exit 4
fi

# 1. Post the top-level PR comment.
TOP_COMMENT_URL=$(gh pr comment "$PR_NUMBER" \
  --repo "$OWNER/$REPO" \
  --body-file "$REVIEW_MD_PATH" 2>/dev/null | tail -1 || true)

if [[ -z "$TOP_COMMENT_URL" ]]; then
  echo '{"error": "gh pr comment did not return a URL"}' >&2
  exit 5
fi

# 2. Post inline comments if a non-empty JSON file is provided.
INLINE_POSTED=0
INLINE_FAILED=0
INLINE_REVIEW_URL=""

if [[ -n "$INLINE_JSON_PATH" && -f "$INLINE_JSON_PATH" ]]; then
  # Resolve the head commit SHA for this PR.
  HEAD_SHA=$(gh pr view "$PR_NUMBER" --repo "$OWNER/$REPO" --json headRefOid -q .headRefOid)

  if [[ -z "$HEAD_SHA" ]]; then
    echo '{"warning": "Could not resolve head SHA; skipping inline comments."}' >&2
  else
    # Build a single review payload from the inline_comments array. GitHub's
    # /pulls/{n}/reviews endpoint accepts a comments[] array in one request.
    PAYLOAD=$(python3 - "$INLINE_JSON_PATH" "$HEAD_SHA" <<'PY'
import json, sys
inline_path, head_sha = sys.argv[1:3]
try:
    with open(inline_path) as f:
        inline = json.load(f)
except Exception as e:
    print(json.dumps({"error": f"failed to read inline comments: {e}"}))
    sys.exit(1)
if not isinstance(inline, list) or not inline:
    print(json.dumps({"empty": True}))
    sys.exit(0)
review_comments = []
for entry in inline:
    if not entry.get("file") or not entry.get("line"):
        continue
    review_comments.append({
        "path": entry["file"],
        "line": int(entry["line"]),
        "side": "RIGHT",
        "body": entry.get("body", ""),
    })
payload = {
    "commit_id": head_sha,
    "event": "COMMENT",
    "body": "Multi-agent PR review — inline findings.",
    "comments": review_comments,
}
print(json.dumps(payload))
PY
    )

    # Detect early-out conditions.
    if echo "$PAYLOAD" | python3 -c "import sys, json; d=json.load(sys.stdin); sys.exit(0 if d.get('empty') or d.get('error') else 1)" 2>/dev/null; then
      :  # nothing to post (empty array or read error already noted)
    else
      # Post the review with all inline comments in one request.
      INLINE_RESPONSE=$(echo "$PAYLOAD" | gh api \
        --method POST \
        "repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews" \
        --input - 2>&1 || true)
      INLINE_REVIEW_URL=$(echo "$INLINE_RESPONSE" | python3 -c "import sys, json; \
        d=json.loads(sys.stdin.read()); print(d.get('html_url', ''))" 2>/dev/null || echo "")
      INLINE_POSTED=$(echo "$PAYLOAD" | python3 -c "import sys, json; print(len(json.load(sys.stdin).get('comments', [])))")
      if [[ -z "$INLINE_REVIEW_URL" ]]; then
        INLINE_FAILED="$INLINE_POSTED"
        INLINE_POSTED=0
      fi
    fi
  fi
fi

# Emit a JSON object on stdout — conductor auto-merges into output.*
python3 - "$TOP_COMMENT_URL" "$INLINE_REVIEW_URL" "$INLINE_POSTED" "$INLINE_FAILED" <<'PY'
import json, sys
top, inline_url, posted, failed = sys.argv[1:5]
print(json.dumps({
    "posted_comment_url": top.strip(),
    "posted_inline_review_url": inline_url.strip(),
    "inline_comments_posted": int(posted),
    "inline_comments_failed": int(failed),
    "poster_status": "success",
}))
PY
