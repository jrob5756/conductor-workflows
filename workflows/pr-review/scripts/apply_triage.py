#!/usr/bin/env python3
"""Apply the human's triage decisions to the findings, before any model sees them.

The writing step must never receive a finding the human dropped. An agent told
"do not mention these" can still leak one into a summary line, and the person
whose pull request it is has no way to tell that happened. Filtering here makes
it impossible rather than discouraged, and it is the same reason
`build_questions` is a script: the guarantees the human relies on do not belong
in a prompt.

Answers join to findings on `id`. The outcomes are the three choices plus the
free-text path, so anything that is not one of the three known choices is
treated as the human's own wording instruction and travels with the finding.
The choice strings come from `triage_choices.py`, so this and the two question
builders cannot disagree about what an answer meant.

The two severity choices change what a finding is filed as, not what it says.
The wording the reviewer produced is posted unchanged at the other weight, and
the approved list is re-sorted so blocking findings still lead the review.

An unanswered or unasked finding is kept, matching the question's declared
default of "Post as-is" and the workflow's rule that skipping posts a finding
rather than dropping it.

Both triage nodes run this: the first-pass one over `build_questions` findings
and the follow-up one over `build_followup_questions` findings. Keys the
builders add beyond the ones read here — a follow-up item's `status`, say —
are copied onto the approved entry untouched.

Usage:
    apply_triage.py            # {"findings": [...], "items": [...]} on stdin

Output:
    ok, error, approved, approved_count, dropped_count, blocking_count,
    recommended_count, reclassified_count, anchored_count
"""

from __future__ import annotations

import json
import sys

from triage_choices import (
    BLOCKING,
    DO_NOT_POST,
    POST_AS_IS,
    RESEVERITY,
)


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))
    sys.exit(0)


def fail(message: str) -> None:
    emit(
        {
            "ok": False,
            "error": message,
            "approved": [],
            "approved_count": 0,
            "dropped_count": 0,
            "blocking_count": 0,
            "recommended_count": 0,
            "reclassified_count": 0,
            "anchored_count": 0,
        }
    )


def decisions_from(items: object) -> dict[str, str]:
    """Index the questions node's answers by question id."""
    decisions: dict[str, str] = {}
    if not isinstance(items, list):
        return decisions
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        if item.get("skipped"):
            continue
        answer = item.get("answer")
        if answer is None:
            continue
        decisions[item_id] = str(answer).strip()
    return decisions


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        fail("No triage payload was provided on stdin")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"Triage payload is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        fail(f"Triage payload must be a JSON object, got {type(parsed).__name__}")

    findings = parsed.get("findings")
    if not isinstance(findings, list):
        fail("Triage payload has no findings array")

    decisions = decisions_from(parsed.get("items"))

    approved: list[dict[str, object]] = []
    dropped = 0
    reclassified = 0
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        answer = decisions.get(str(finding.get("id") or ""), POST_AS_IS)
        normalized = answer.casefold()
        if normalized == DO_NOT_POST.casefold():
            dropped += 1
            continue
        entry = dict(finding)
        severity = RESEVERITY.get(normalized)
        if severity is not None:
            if entry.get("severity") != severity:
                reclassified += 1
            entry["severity"] = severity
            entry["guidance"] = ""
        else:
            # Free text is the reword path, so anything that is neither the
            # "as-is" choice nor a severity choice is an instruction about how
            # to word this finding.
            entry["guidance"] = "" if normalized == POST_AS_IS.casefold() else answer
        approved.append(entry)

    # `build_questions` ordered these blocking first, and a reclassification
    # breaks that. Restoring it keeps the review body ordered by weight rather
    # than by what the reviewer happened to call each finding. The sort is
    # stable, so findings that were not touched keep their original order.
    approved.sort(key=lambda f: 0 if f.get("severity") == BLOCKING else 1)

    blocking = sum(1 for f in approved if f.get("severity") == BLOCKING)
    anchored = sum(1 for f in approved if f.get("path") and f.get("line"))

    emit(
        {
            "ok": True,
            "error": "",
            "approved": approved,
            "approved_count": len(approved),
            "dropped_count": dropped,
            "blocking_count": blocking,
            "recommended_count": len(approved) - blocking,
            "reclassified_count": reclassified,
            "anchored_count": anchored,
        }
    )


if __name__ == "__main__":
    main()
