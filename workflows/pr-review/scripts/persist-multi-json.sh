#!/usr/bin/env bash
#
# persist-multi-json.sh — Write multiple JSON strings to multiple files in
# a single script invocation.
#
# Used by sub-workflows that need to persist several raw outputs at the end
# of their run (e.g. specialist outputs + gate output + contributions).
#
# Usage:
#   persist-multi-json.sh <base_dir> <name1> <json1> [<name2> <json2> ...]
#
# Behavior:
#   - Creates base_dir if missing.
#   - For each name/json pair, validates JSON and pretty-prints to base_dir/name.
#   - Aborts on the FIRST invalid JSON pair so the workflow doesn't carry
#     half-written intermediate files into downstream agents.
#   - Emits a JSON summary on stdout listing the files written.

set -euo pipefail

BASE_DIR="${1:-}"
shift || true

if [[ -z "$BASE_DIR" ]]; then
  echo '{"error": "persist-multi-json.sh requires <base_dir> <name1> <json1> ..."}' >&2
  exit 1
fi

if [[ "$#" -eq 0 || $(( $# % 2 )) -ne 0 ]]; then
  echo '{"error": "persist-multi-json.sh requires an even number of name/json arguments after base_dir"}' >&2
  exit 1
fi

mkdir -p "$BASE_DIR"

# Iterate name/json pairs through python for validation + pretty-printing.
python3 - "$BASE_DIR" "$@" <<'PY'
import json, os, sys
base_dir = sys.argv[1]
pairs = sys.argv[2:]
written = []
for i in range(0, len(pairs), 2):
    name, raw = pairs[i], pairs[i + 1]
    if not raw:
        raw = "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON for {name}: {e}"}), file=sys.stderr)
        sys.exit(2)
    target = os.path.join(base_dir, name)
    with open(target, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    written.append({"name": name, "path": target, "bytes": len(raw)})
print(json.dumps({"written": written}))
PY
