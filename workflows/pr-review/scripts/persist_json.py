#!/usr/bin/env python3
"""persist_json.py — Write a JSON string to a target file path.

Used by sub-workflows to persist their raw outputs (specialist findings, lens
deliberation, polish output, etc.) to pr_data_dir so the render sub-workflow
can read them via MCP filesystem instead of receiving them all through
input_mapping.

Usage:
    persist_json.py <target_path> <json_string>

Behavior:
    - Creates the parent directory if missing.
    - Validates that the JSON string is parseable; aborts on bad JSON so
      downstream agents never read corrupt data.
    - Pretty-prints with 2-space indent for human-readable archives.

Exit codes:
    0  success
    1  missing arguments
    2  invalid JSON in <json_string>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("target_path")
    parser.add_argument("json_string", nargs="?", default="")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(
            json.dumps(
                {"error": "persist_json.py requires <target_path> <json_string>"}
            ),
            file=sys.stderr,
        )
        return 1

    target = Path(args.target_path)
    raw = args.json_string or "{}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(
            json.dumps({"error": f"persist_json.py received invalid JSON: {e}"}),
            file=sys.stderr,
        )
        return 2

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(json.dumps({"persisted": str(target), "bytes": len(raw)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
