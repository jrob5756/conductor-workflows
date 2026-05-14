#!/usr/bin/env bash
#
# persist-json.sh — Write a JSON string to a target file path.
#
# Used by sub-workflows to persist their raw outputs (specialist findings,
# lens deliberation, polish output, etc.) to pr_data_dir so the render
# sub-workflow can read them via MCP filesystem instead of receiving them
# all through input_mapping.
#
# Usage:
#   persist-json.sh <target_path> <json_string>
#
# Behavior:
#   - Creates the parent directory if missing.
#   - Validates that the JSON string is parseable; aborts on bad JSON so
#     downstream agents never read corrupt data.
#   - Pretty-prints with 2-space indent for human-readable archives.

set -euo pipefail

TARGET_PATH="${1:-}"
JSON_STRING="${2:-}"

if [[ -z "$TARGET_PATH" ]]; then
  echo '{"error": "persist-json.sh requires <target_path> <json_string>"}' >&2
  exit 1
fi

if [[ -z "$JSON_STRING" ]]; then
  # Allow explicitly empty objects/arrays — write "{}" rather than fail.
  JSON_STRING='{}'
fi

mkdir -p "$(dirname "$TARGET_PATH")"

# Validate + pretty-print via python.
python3 - "$TARGET_PATH" "$JSON_STRING" <<'PY'
import json, sys
target_path, raw = sys.argv[1:3]
try:
    data = json.loads(raw)
except json.JSONDecodeError as e:
    print(json.dumps({"error": f"persist-json.sh received invalid JSON: {e}"}), file=sys.stderr)
    sys.exit(2)
with open(target_path, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
print(json.dumps({"persisted": target_path, "bytes": len(raw)}))
PY
