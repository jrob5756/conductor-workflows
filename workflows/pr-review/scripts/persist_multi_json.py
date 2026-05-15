#!/usr/bin/env python3
"""persist_multi_json.py — Write multiple JSON strings to multiple files.

Used by sub-workflows that need to persist several raw outputs at the end of
their run (e.g. specialist outputs + gate output + contributions).

Usage:
    persist_multi_json.py <base_dir> <name1> <json1> [<name2> <json2> ...]

Behavior:
    - Creates base_dir if missing.
    - For each name/json pair, validates JSON and pretty-prints to base_dir/name.
    - Aborts on the FIRST invalid JSON pair so the workflow doesn't carry
      half-written intermediate files into downstream agents.
    - Emits a JSON summary on stdout listing the files written.

Exit codes:
    0  success
    1  missing/odd arguments
    2  invalid JSON in any pair
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        print(
            json.dumps(
                {
                    "error": "persist_multi_json.py requires "
                    "<base_dir> <name1> <json1> [<name2> <json2> ...]"
                }
            ),
            file=sys.stderr,
        )
        return 1

    base_dir = Path(argv[0])
    pairs = argv[1:]
    if len(pairs) == 0 or len(pairs) % 2 != 0:
        print(
            json.dumps(
                {
                    "error": "persist_multi_json.py requires an even number "
                    "of name/json arguments after base_dir"
                }
            ),
            file=sys.stderr,
        )
        return 1

    base_dir.mkdir(parents=True, exist_ok=True)

    written: list[dict[str, object]] = []
    for i in range(0, len(pairs), 2):
        name = pairs[i]
        raw = pairs[i + 1] or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(
                json.dumps({"error": f"invalid JSON for {name}: {e}"}),
                file=sys.stderr,
            )
            return 2
        target = base_dir / name
        with target.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        written.append({"name": name, "path": str(target), "bytes": len(raw)})

    print(json.dumps({"written": written}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
