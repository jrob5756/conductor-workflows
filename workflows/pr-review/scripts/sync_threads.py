#!/usr/bin/env python3
"""Confirm fixed points on their original review threads and resolve them.

Stdin: {"confirmations": [{thread_id, comment_id, body, resolve}]}.
Used on the paths that publish no follow-up review, so a fix the author made
is still acknowledged. Failures are reported, never fatal.
"""

from __future__ import annotations

import json
import sys

from threads import apply_actions, valid_action


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))
    sys.exit(0)


def main(argv: list[str]) -> None:
    if len(argv) < 2 or not argv[1].isdigit():
        emit({"ok": False, "error": "sync_threads.py requires name_with_owner and pr_number", "synced": 0, "errors": []})
    try:
        payload = json.loads(sys.stdin.read().strip() or "{}")
    except json.JSONDecodeError as exc:
        emit({"ok": False, "error": f"Thread payload is not valid JSON: {exc}", "synced": 0, "errors": []})
    actions = payload.get("confirmations", []) if isinstance(payload, dict) else None
    if not isinstance(actions, list) or not all(valid_action(a) for a in actions):
        emit({"ok": False, "error": "Malformed thread confirmations.", "synced": 0, "errors": []})
    done, errors = apply_actions(argv[0], argv[1], actions)
    emit({"ok": not errors, "error": "; ".join(errors), "synced": done, "errors": errors})


if __name__ == "__main__":
    main(sys.argv[1:])
