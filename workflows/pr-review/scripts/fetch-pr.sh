#!/usr/bin/env bash
#
# fetch-pr.sh — Fetch GitHub PR data using the gh CLI.
#
# Writes metadata.json, diff.patch, and changed-files.json to a directory.
# Emits a JSON object on stdout that conductor auto-merges into the
# script agent's output.* fields.
#
# Usage:
#   fetch-pr.sh <pr_url> [output_dir_override]
#
# Arguments:
#   pr_url               — https://github.com/<owner>/<repo>/pull/<number>
#   output_dir_override  — optional; if empty, a fresh mktemp dir is used.

set -euo pipefail

PR_URL="${1:-}"
OUTPUT_DIR_OVERRIDE="${2:-}"

if [[ -z "$PR_URL" ]]; then
  echo '{"error": "pr_url argument is required"}' >&2
  exit 1
fi

# Validate gh is available and authenticated.
if ! command -v gh >/dev/null 2>&1; then
  echo '{"error": "gh CLI not found on PATH. Install from https://cli.github.com/ and run `gh auth login`."}' >&2
  exit 2
fi
if ! gh auth status >/dev/null 2>&1; then
  echo '{"error": "gh CLI is not authenticated. Run `gh auth login` first."}' >&2
  exit 3
fi

# Validate URL shape: https://github.com/<owner>/<repo>/pull/<number>[/]
if [[ ! "$PR_URL" =~ ^https://github\.com/([^/]+)/([^/]+)/pull/([0-9]+)/?$ ]]; then
  echo "{\"error\": \"Invalid GitHub PR URL: $PR_URL. Expected: https://github.com/owner/repo/pull/123\"}" >&2
  exit 4
fi
OWNER="${BASH_REMATCH[1]}"
REPO="${BASH_REMATCH[2]}"
NUMBER="${BASH_REMATCH[3]}"

# Resolve output directory.
if [[ -n "$OUTPUT_DIR_OVERRIDE" ]]; then
  PR_DATA_DIR="$OUTPUT_DIR_OVERRIDE"
else
  PR_DATA_DIR="$(mktemp -d -t pr-review-XXXXXX)"
fi
mkdir -p "$PR_DATA_DIR"

# Fetch metadata (single JSON blob).
gh pr view "$NUMBER" \
  --repo "$OWNER/$REPO" \
  --json number,title,body,author,baseRefName,headRefName,baseRefOid,headRefOid,url,state,isDraft,labels,additions,deletions,changedFiles \
  > "$PR_DATA_DIR/metadata-raw.json"

# Augment metadata with platform marker + owner/repo for downstream agents.
python3 - "$PR_DATA_DIR/metadata-raw.json" "$PR_DATA_DIR/metadata.json" "$OWNER" "$REPO" <<'PY'
import json, sys
src, dst, owner, repo = sys.argv[1:5]
with open(src) as f:
    data = json.load(f)
data["platform"] = "github"
data["owner"] = owner
data["repo"] = repo
# Normalise author: gh returns {"login": "x", ...}; expose flat string too.
if isinstance(data.get("author"), dict):
    data["author_login"] = data["author"].get("login", "")
with open(dst, "w") as f:
    json.dump(data, f, indent=2)
PY

# Fetch unified diff.
gh pr diff "$NUMBER" --repo "$OWNER/$REPO" > "$PR_DATA_DIR/diff.patch"

# Fetch the per-file changed files list (filename, status, additions, deletions, patch).
gh api "repos/$OWNER/$REPO/pulls/$NUMBER/files" \
  --paginate \
  > "$PR_DATA_DIR/changed-files.json"

# Emit a JSON object on stdout. Conductor auto-merges these fields into
# pr_fetcher.output.* for downstream agents.
python3 - "$PR_DATA_DIR" "$PR_URL" "$OWNER" "$REPO" "$NUMBER" <<'PY'
import json, os, sys
pr_data_dir, pr_url, owner, repo, number = sys.argv[1:6]
with open(os.path.join(pr_data_dir, "metadata.json")) as f:
    meta = json.load(f)
out = {
    "pr_data_dir": pr_data_dir,
    "pr_url": pr_url,
    "owner": owner,
    "repo": repo,
    "pr_number": int(number),
    "pr_title": meta.get("title", ""),
    "platform": "github",
    "fetcher_status": "success",
}
print(json.dumps(out))
PY
