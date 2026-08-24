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
currently carry, so one tap moves it between blocking and recommended.

Usage:
    build_questions.py            # findings JSON array on stdin

Output:
    ok, error, questions, findings, question_count, blocking_count,
    recommended_count, nits_dropped
"""

from __future__ import annotations

import json
import sys

POST_AS_IS = "Post as-is"
DO_NOT_POST = "Do not post"
POST_AS_BLOCKING = "Post as BLOCKING"
POST_AS_RECOMMENDED = "Post as RECOMMENDED"

# `apply_triage.py` compares answers against these exact strings, and the
# triage prompt explains them in the same words. Change one and change all
# three, or a decision stops meaning what the human was told it meant.

BLOCKING = "BLOCKING"
RECOMMENDED = "RECOMMENDED"

# Only the opposite severity is offered. A choice that restates the label the
# finding already carries is a no-op the human has to read past on every
# question, and it makes "did I change this one?" unanswerable from the answer.
FLIP_CHOICE = {BLOCKING: POST_AS_RECOMMENDED, RECOMMENDED: POST_AS_BLOCKING}

# A reviewer told to emit BLOCKING or RECOMMENDED still reaches for its own
# vocabulary sometimes. Mapping the escalating words onto BLOCKING matters far
# more than the reverse: silently filing a CRITICAL finding as a suggestion is
# the one normalization error that loses real severity.
ESCALATING = ("block", "critical", "severe", "major", "high", "must", "error", "bug")
DISCARDED = ("nit", "trivial", "cosmetic", "style")

TITLE_LIMIT = 160


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


def text_of(value: object) -> str:
    """Coerce a field to trimmed text, tolerating a model's stray null or number."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def line_of(value: object) -> int:
    """Coerce a line number, returning 0 when there is no usable anchor."""
    try:
        line = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return line if line > 0 else 0


def severity_of(value: object) -> str:
    """Normalize a severity label.

    Returns an empty string for a nit, which is dropped: `code-review` ranks
    those separately and they are not worth a decision each.
    """
    label = text_of(value).lower()
    if any(label.startswith(word) for word in DISCARDED):
        return ""
    if any(word in label for word in ESCALATING):
        return BLOCKING
    return RECOMMENDED


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
