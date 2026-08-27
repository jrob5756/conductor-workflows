#!/usr/bin/env python3
"""Turn code-review findings into questions the triage node can present.

This is a script rather than part of the reviewing agent's output contract
because Conductor's `QuestionDef` forbids unknown keys: a single stray field on
one entry aborts the questions node at runtime, which is *after* an expensive
review has already been paid for. Deriving the questions here guarantees the
shape, and re-keying the findings guarantees the ids are unique and ordered.

Both arrays are emitted. They join on `id`, and downstream steps read the
normalized `findings` here rather than the reviewer's raw output.

The triage outcomes come from three choices plus the free-text path: a choice
reading "reword it" would record that label and no wording, since choices carry
no follow-up prompt. The third choice is the severity the finding does *not*
currently carry, so one tap moves it between blocking and recommended. The
choice strings themselves live in `triage_choices.py`, shared with the script
that reads the answers back.

Usage:
    build_questions.py            # findings JSON array on stdin

Output:
    ok, error, questions, findings, question_count, blocking_count,
    recommended_count, nits_dropped
"""

from __future__ import annotations

import json
import sys

from triage_choices import (
    BLOCKING,
    DO_NOT_POST,
    FLIP_CHOICE,
    POST_AS_IS,
    RECOMMENDED,
    TITLE_LIMIT,
    line_of,
    severity_of,
    text_of,
)


def answer_guidance(severity: str) -> str:
    """The three choices spelled out, naming the severity this finding can move to."""
    flip = FLIP_CHOICE[severity]
    return (
        f"Choose '{POST_AS_IS}' to post this wording at {severity}, "
        f"'{flip}' to post the same wording at the other severity, "
        f"'{DO_NOT_POST}' to drop it, or write your own note to post it with "
        "your changes applied."
    )


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))
    sys.exit(0)


def fail(message: str) -> None:
    emit(
        {
            "ok": False,
            "error": message,
            "questions": [],
            "findings": [],
            "question_count": 0,
            "blocking_count": 0,
            "recommended_count": 0,
            "nits_dropped": 0,
        }
    )


def normalize(entry: object) -> tuple[dict[str, object] | None, str]:
    """Reduce one raw finding to the fields downstream steps rely on.

    Returns `(finding, reason)`. The finding is None when the entry cannot
    become a decision worth a human's time, and the reason says why: `nit` for
    one that was deliberately filtered, `invalid` for one that was malformed.
    The two are counted separately so a reviewer whose output could not be
    parsed is never reported as a review that found nothing.
    """
    if isinstance(entry, str):
        body = entry.strip()
        if not body:
            return None, "invalid"
        return {
            "severity": RECOMMENDED,
            "path": "",
            "line": 0,
            "title": body.splitlines()[0][:TITLE_LIMIT],
            "body": body,
            "suggestion": "",
        }, ""

    if not isinstance(entry, dict):
        return None, "invalid"

    severity = severity_of(entry.get("severity"))
    if not severity:
        return None, "nit"

    body = text_of(entry.get("body")) or text_of(entry.get("description"))
    title = text_of(entry.get("title")) or (body.splitlines()[0] if body else "")
    if not title:
        return None, "invalid"
    if not body:
        body = title

    return {
        "severity": severity,
        "path": text_of(entry.get("path")) or text_of(entry.get("file")),
        "line": line_of(entry.get("line")),
        "title": title[:TITLE_LIMIT],
        "body": body,
        "suggestion": text_of(entry.get("suggestion")),
    }, ""


def location_of(finding: dict[str, object]) -> str:
    path = finding["path"]
    if not path:
        return ""
    line = finding["line"]
    return f"{path}:{line}" if line else str(path)


def question_for(finding: dict[str, object]) -> dict[str, object]:
    """Build one strictly `QuestionDef`-shaped entry. No other keys may appear."""
    severity = str(finding["severity"])
    location = location_of(finding)
    suffix = f" ({location})" if location else ""
    hint_parts = [str(finding["body"])]
    if finding["suggestion"]:
        hint_parts.append(f"Suggested fix: {finding['suggestion']}")
    hint_parts.append(answer_guidance(severity))

    return {
        "id": finding["id"],
        "text": f"{severity}: {finding['title']}{suffix}",
        "hint": "\n\n".join(hint_parts),
        "choices": [POST_AS_IS, FLIP_CHOICE[severity], DO_NOT_POST],
        "allow_free_text": True,
        "default": POST_AS_IS,
        "required": False,
        "multiline": True,
    }


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        emit(
            {
                "ok": True,
                "error": "",
                "questions": [],
                "findings": [],
                "question_count": 0,
                "blocking_count": 0,
                "recommended_count": 0,
                "nits_dropped": 0,
            }
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"Findings are not valid JSON: {exc}")

    if parsed is None:
        parsed = []
    if not isinstance(parsed, list):
        fail(f"Findings must be a JSON array, got {type(parsed).__name__}")

    normalized: list[dict[str, object]] = []
    invalid = 0
    nits = 0
    for entry in parsed:
        finding, reason = normalize(entry)
        if finding is not None:
            normalized.append(finding)
        elif reason == "nit":
            nits += 1
        else:
            invalid += 1

    if invalid:
        fail(
            f"{invalid} of {len(parsed)} findings could not be read. A finding "
            "the reviewer produced but this step cannot parse would be dropped "
            "without anyone deciding to drop it, so the run stops instead."
        )

    blocking = [f for f in normalized if f["severity"] == BLOCKING]
    recommended = [f for f in normalized if f["severity"] == RECOMMENDED]

    findings: list[dict[str, object]] = []
    for prefix, group in (("b", blocking), ("r", recommended)):
        for index, finding in enumerate(group, 1):
            finding["id"] = f"{prefix}{index}"
            findings.append(finding)

    emit(
        {
            "ok": True,
            "error": "",
            "questions": [question_for(f) for f in findings],
            "findings": findings,
            "question_count": len(findings),
            "blocking_count": len(blocking),
            "recommended_count": len(recommended),
            "nits_dropped": nits,
        }
    )


if __name__ == "__main__":
    main()
