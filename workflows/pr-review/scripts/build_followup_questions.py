#!/usr/bin/env python3
"""Turn a follow-up analysis into questions about what to raise again.

Same job as `build_questions.py`, and a script for the same reason: Conductor's
`QuestionDef` forbids unknown keys, so deriving the questions here rather than
asking the analysis agent to emit them means a stray field cannot abort the
triage node after the analysis has been paid for.

What differs is the filter. A first-pass review drops nits because they are not
worth a decision each. This one drops nothing on severity: every entry here is
a point you already made once, in public, and quietly withholding it the second
time would leave the author reading agreement into silence. Only an item the
analysis found *closed* — fixed, or made moot by the change moving on — skips
the questions, and those are still counted and reported so "everything I asked
for is done" is a result you can see rather than infer from an empty list.

An unrecognised status becomes `unclear`, which is outstanding. A malformed
entry fails the step. Both defaults run the same way: toward putting the point
in front of you rather than dropping it on your behalf.

The choices are the ones `triage_choices.py` defines, so `apply_triage.py`
reads these answers with no follow-up-specific branch.

Usage:
    build_followup_questions.py    # {"items": [...], "prior_items": [...]} on
                                   # stdin; a bare array is the items alone

Output:
    ok, error, questions, findings, resolved, question_count, blocking_count,
    recommended_count, outstanding_count, addressed_count, obsolete_count,
    unaccounted_count, unsourced_count
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

NOT_ADDRESSED = "not_addressed"
PARTIALLY_ADDRESSED = "partially_addressed"
UNCLEAR = "unclear"
ADDRESSED = "addressed"
OBSOLETE = "obsolete"

OUTSTANDING = (NOT_ADDRESSED, PARTIALLY_ADDRESSED, UNCLEAR)
CLOSED = (ADDRESSED, OBSOLETE)

STATUS_LABEL = {
    NOT_ADDRESSED: "Still open",
    PARTIALLY_ADDRESSED: "Partly done",
    UNCLEAR: "Could not tell",
    ADDRESSED: "Addressed",
    OBSOLETE: "Moot",
}

RESOLVED_LIMIT = 25


def answer_guidance(severity: str) -> str:
    """The three choices spelled out, in the vocabulary of a second pass."""
    flip = FLIP_CHOICE[severity]
    return (
        f"Choose '{POST_AS_IS}' to raise it again at {severity}, "
        f"'{flip}' to raise the same wording at the other severity, "
        f"'{DO_NOT_POST}' to let it go, or write your own note to raise it "
        "with your changes applied."
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
            "resolved": [],
            "question_count": 0,
            "blocking_count": 0,
            "recommended_count": 0,
            "outstanding_count": 0,
            "addressed_count": 0,
            "obsolete_count": 0,
            "unaccounted_count": 0,
            "unsourced_count": 0,
        }
    )


def status_of(value: object) -> str:
    """Normalize a status label, defaulting to `unclear` rather than to closed.

    The negative forms are tested before the positive ones because
    "not addressed" contains "addressed", and reading that as done is the one
    normalization error that drops a live finding.
    """
    label = text_of(value).lower().replace("-", "_").replace(" ", "_")
    if label in OUTSTANDING or label in CLOSED:
        return label
    if "partial" in label:
        return PARTIALLY_ADDRESSED
    if any(word in label for word in ("unclear", "unknown", "uncertain", "unsure", "cannot")):
        return UNCLEAR
    # Negatives are tested before positives because "unresolved" contains
    # "resolved" and "unfixed" contains "fixed". Reading either as done is the
    # one normalization error that closes a live point without a decision.
    if (
        label.startswith(("not", "un", "in"))
        or "not_" in label
        or any(
            word in label
            for word in ("outstanding", "open", "pending", "missing", "remain", "still", "ignored")
        )
    ):
        return NOT_ADDRESSED
    if any(word in label for word in ("obsolete", "moot", "withdraw", "no_longer")):
        return OBSOLETE
    if any(word in label for word in ("address", "fixed", "resolved", "done", "closed", "complete")):
        return ADDRESSED
    return UNCLEAR


def normalize(entry: object) -> dict[str, object] | None:
    """Reduce one analysis entry to the fields the rest of the pipeline reads.

    Returns None when the entry carries no title and no text at all, which is
    the only shape that cannot become either a question or a count.
    """
    if not isinstance(entry, dict):
        return None

    original = text_of(entry.get("original")) or text_of(entry.get("body"))
    evidence = text_of(entry.get("evidence"))
    recommendation = text_of(entry.get("recommendation")) or text_of(entry.get("suggestion"))
    title = text_of(entry.get("title"))
    if not title and original:
        title = original.splitlines()[0]
    if not title:
        return None

    parts = [original] if original else []
    if evidence:
        parts.append(f"What the code shows now: {evidence}")

    # A severity the analysis filed as a nit is kept as RECOMMENDED rather than
    # dropped. `build_questions` can drop a nit because nobody has seen it yet;
    # here the author has, and withholding the second mention is a decision the
    # human should make.
    return {
        "status": status_of(entry.get("status")),
        "severity": severity_of(entry.get("severity")) or RECOMMENDED,
        "path": text_of(entry.get("path")) or text_of(entry.get("file")),
        "line": line_of(entry.get("line")),
        "title": title[:TITLE_LIMIT],
        "body": "\n\n".join(parts) or title,
        "suggestion": recommendation,
        "source_id": text_of(entry.get("source_id")) or text_of(entry.get("id")),
    }


UNEXAMINED_NOTE = (
    "The follow-up analysis did not account for this point, so it reaches you "
    "unexamined. Check it yourself before deciding."
)


def synthesize(prior: object) -> dict[str, object] | None:
    """Rebuild a question from a prior comment the analysis never mentioned.

    An omitted point is the failure this cannot afford: nobody decided to drop
    it, and the gate downstream would go on to say every point was settled.
    Filing it `unclear` puts it back in front of the human with its original
    text, which is the safe direction and the one the human can correct.
    """
    if not isinstance(prior, dict):
        return None
    body = text_of(prior.get("body"))
    if not body:
        return None
    return {
        "status": UNCLEAR,
        "severity": RECOMMENDED,
        "path": text_of(prior.get("path")),
        "line": line_of(prior.get("line")),
        "title": body.splitlines()[0][:TITLE_LIMIT],
        "body": f"{body}\n\n{UNEXAMINED_NOTE}",
        "suggestion": "",
        "source_id": text_of(prior.get("id")),
    }


def location_of(finding: dict[str, object]) -> str:
    path = finding["path"]
    if not path:
        return ""
    line = finding["line"]
    return f"{path}:{line}" if line else str(path)


def question_for(finding: dict[str, object]) -> dict[str, object]:
    """Build one strictly `QuestionDef`-shaped entry. No other keys may appear."""
    severity = str(finding["severity"])
    status = str(finding["status"])
    location = location_of(finding)
    suffix = f" ({location})" if location else ""
    hint_parts = [str(finding["body"])]
    if finding["suggestion"]:
        hint_parts.append(f"What is still needed: {finding['suggestion']}")
    hint_parts.append(answer_guidance(severity))

    return {
        "id": finding["id"],
        "text": f"{STATUS_LABEL[status]} — {severity}: {finding['title']}{suffix}",
        "hint": "\n\n".join(hint_parts),
        "choices": [POST_AS_IS, FLIP_CHOICE[severity], DO_NOT_POST],
        "allow_free_text": True,
        "default": POST_AS_IS,
        "required": False,
        "multiline": True,
    }


EMPTY = {
    "ok": True,
    "error": "",
    "questions": [],
    "findings": [],
    "resolved": [],
    "question_count": 0,
    "blocking_count": 0,
    "recommended_count": 0,
    "outstanding_count": 0,
    "addressed_count": 0,
    "obsolete_count": 0,
    "unaccounted_count": 0,
    "unsourced_count": 0,
}


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        emit(dict(EMPTY))

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"The follow-up analysis is not valid JSON: {exc}")

    # A bare array is the analysis alone. The object form carries the prior
    # comments too, which is what lets an omitted point be caught.
    if isinstance(payload, list):
        entries, prior_items = payload, []
    elif isinstance(payload, dict):
        entries = payload.get("items") or []
        prior_items = payload.get("prior_items") or []
    elif payload is None:
        entries, prior_items = [], []
    else:
        fail(f"The follow-up payload must be an array or object, got {type(payload).__name__}")

    if not isinstance(entries, list):
        fail(f"The follow-up analysis must be a JSON array, got {type(entries).__name__}")
    if not isinstance(prior_items, list):
        prior_items = []

    normalized: list[dict[str, object]] = []
    invalid = 0
    for entry in entries:
        item = normalize(entry)
        if item is None:
            invalid += 1
        else:
            normalized.append(item)

    if invalid:
        fail(
            f"{invalid} of {len(entries)} analysed items could not be read. An "
            "item you raised before that this step cannot parse would be "
            "dropped without anyone deciding to drop it, so the run stops "
            "instead."
        )

    cited = {str(item["source_id"]) for item in normalized if item["source_id"]}
    known = {
        text_of(prior.get("id"))
        for prior in prior_items
        if isinstance(prior, dict) and text_of(prior.get("id"))
    }
    unaccounted = 0
    for prior in prior_items:
        if not isinstance(prior, dict) or text_of(prior.get("id")) in cited:
            continue
        recovered = synthesize(prior)
        if recovered is not None:
            normalized.append(recovered)
            unaccounted += 1

    outstanding = [item for item in normalized if item["status"] in OUTSTANDING]
    resolved = [item for item in normalized if item["status"] in CLOSED]

    blocking = [item for item in outstanding if item["severity"] == BLOCKING]
    recommended = [item for item in outstanding if item["severity"] == RECOMMENDED]

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
            "resolved": [
                f"{STATUS_LABEL[str(item['status'])]}: {item['title']}"
                for item in resolved[:RESOLVED_LIMIT]
            ],
            "question_count": len(findings),
            "blocking_count": len(blocking),
            "recommended_count": len(recommended),
            "outstanding_count": len(outstanding),
            "addressed_count": sum(1 for item in resolved if item["status"] == ADDRESSED),
            "obsolete_count": sum(1 for item in resolved if item["status"] == OBSOLETE),
            "unaccounted_count": unaccounted,
            # An entry citing no prior comment, or one that does not exist, is
            # a point the analysis introduced rather than followed up. It is
            # still asked — the human drops what does not belong — but the
            # count says it happened.
            "unsourced_count": sum(
                1
                for item in normalized
                if known and str(item["source_id"]) not in known
            ),
        }
    )


if __name__ == "__main__":
    main()
