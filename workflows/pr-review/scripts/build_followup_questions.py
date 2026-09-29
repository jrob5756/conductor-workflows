#!/usr/bin/env python3
"""Reconcile distinct earlier points and replace prior CI with final check results.

Stdin supplies items, prior_items, duplicate_sources and ci_findings. Unexamined
legacy sources fail closed; verified code sources remain visible at triage.
"""

from __future__ import annotations

import json
import sys

from build_questions import normalize as normalize_ci_finding, question_for as ci_question_for
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
NEW = "new"

OUTSTANDING = (NOT_ADDRESSED, PARTIALLY_ADDRESSED, UNCLEAR, NEW)
CLOSED = (ADDRESSED, OBSOLETE)

STATUS_LABEL = {
    NOT_ADDRESSED: "Still open",
    PARTIALLY_ADDRESSED: "Partly done",
    UNCLEAR: "Could not tell",
    NEW: "New",
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
            "ci_count": 0,
            "new_count": 0,
            "confirmations": [],
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
    if "source_ids" in entry:
        sources = entry["source_ids"]
    else:
        if any(key in entry and not isinstance(entry[key], str) for key in ("source_id", "id")):
            return None
        source = text_of(entry.get("source_id")) or text_of(entry.get("id"))
        sources = [source] if source else []
    if (
        not isinstance(sources, list)
        or any(not isinstance(source, str) or not source.strip() for source in sources)
        or len(set(sources)) != len(sources)
        or entry.get("source_type", "code") not in ("code", "ci")
    ):
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
        "evidence": evidence,
        "source_ids": sources,
        "source_id": sources[0] if sources else "",
        "source_type": entry.get("source_type", "code"),
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
        "source_ids": [text_of(prior.get("id"))],
        "source_type": "ci" if prior.get("source_type") == "ci" else "code",
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
    "ci_count": 0,
    "new_count": 0,
    "confirmations": [],
}


def thread_of(item: dict[str, object], by_id: dict[str, dict[str, object]]) -> dict[str, object] | None:
    """The first earlier comment behind this point that still has a readable thread."""
    for source in item["source_ids"]:
        prior = by_id.get(source)
        if prior and isinstance(prior.get("thread_id"), str) and prior["thread_id"] \
                and type(prior.get("comment_id")) is int:
            return prior
    return None


def confirmations_for(
    resolved: list[dict[str, object]],
    outstanding: list[dict[str, object]],
    by_id: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    """Threads whose point is fixed: confirm the fix and resolve, unless another point still lives there."""
    open_threads = set()
    for item in outstanding:
        thread = thread_of(item, by_id)
        if thread:
            open_threads.add(thread["thread_id"])
    actions, seen = [], set()
    for item in resolved:
        thread = thread_of(item, by_id) if item["status"] == ADDRESSED else None
        if not thread or thread["thread_id"] in open_threads | seen:
            continue
        seen.add(thread["thread_id"])
        already_resolved = bool(thread.get("thread_resolved"))
        # A resolved thread whose last word is yours was already confirmed on an earlier visit.
        if already_resolved and not thread.get("awaiting_reply", True):
            continue
        evidence = str(item.get("evidence") or "").strip()
        actions.append({
            "thread_id": thread["thread_id"],
            "comment_id": thread["comment_id"],
            "body": f"Fix confirmed. {evidence}".strip(),
            "resolve": "" if already_resolved else "resolve",
            "path": item["path"],
            "line": item["line"],
            "title": item["title"],
        })
    return actions


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
    ci_entries = []
    duplicates = []
    ci_verified = False
    new_entries = []
    if isinstance(payload, list):
        entries, prior_items = payload, []
    elif isinstance(payload, dict):
        entries = payload.get("items", [])
        prior_items = payload.get("prior_items", [])
        ci_entries = payload.get("ci_findings", [])
        ci_verified = "ci_findings" in payload
        duplicates = payload.get("duplicate_sources", [])
        new_entries = payload.get("new_items", [])
    elif payload is None:
        entries, prior_items = [], []
    else:
        fail(f"The follow-up payload must be an array or object, got {type(payload).__name__}")

    if not isinstance(entries, list):
        fail(f"The follow-up analysis must be a JSON array, got {type(entries).__name__}")
    if not isinstance(ci_entries, list):
        fail("CI findings must be a JSON array.")
    if not isinstance(new_entries, list):
        fail("New findings must be a JSON array.")
    if not isinstance(prior_items, list) or any(
        not isinstance(prior, dict) or not isinstance(prior.get("id"), str)
        or not prior["id"] or not isinstance(prior.get("body"), str) or not prior["body"].strip()
        for prior in prior_items
    ):
        fail("Prior items must contain identified, nonempty feedback.")
    if len({prior["id"] for prior in prior_items}) != len(prior_items):
        fail("Prior item IDs must be unique.")
    if not isinstance(duplicates, list):
        fail("Duplicate-source accounting must be an array.")

    normalized: list[dict[str, object]] = []
    invalid = 0
    for entry in entries:
        item = normalize(entry)
        if item is None:
            invalid += 1
        else:
            normalized.append(item)

    new_items = []
    for entry in new_entries:
        item = normalize(entry) if isinstance(entry, dict) and not entry.get("source_ids") else None
        if item is None or item["source_type"] != "code":
            invalid += 1
        else:
            new_items.append({**item, "status": NEW})

    if invalid:
        fail(
            f"{invalid} of {len(entries)} analysed items could not be read. An "
            "item you raised before that this step cannot parse would be "
            "dropped without anyone deciding to drop it, so the run stops "
            "instead."
        )

    by_id = {prior["id"]: prior for prior in prior_items}
    known = set(by_id)
    cited = {source for item in normalized for source in item["source_ids"]}
    delegated = {source for source, prior in by_id.items()
                 if prior.get("source_type") == "ci"} if ci_verified else set()
    verified_ci = set(delegated)
    retained = []
    for item in normalized:
        sources = set(item["source_ids"])
        prior_ci = sources & verified_ci
        if prior_ci and sources - verified_ci:
            fail("A follow-up point mixes CI and code sources; split them before reconciliation.")
        if ci_verified and (prior_ci or item["source_type"] == "ci"):
            if not sources or not sources <= known:
                fail("A delegated CI point must cite known earlier feedback.")
            if any(by_id[source].get("source_type") == "code" for source in sources):
                fail("A genuine code finding cannot be delegated to CI.")
            delegated.update(sources)
        else:
            retained.append(item)
    normalized = retained
    duplicate_ids = set()
    for duplicate in duplicates:
        if not isinstance(duplicate, dict):
            fail("Malformed duplicate-source accounting.")
        source, covered = duplicate.get("source_id"), duplicate.get("covered_by")
        evidence = duplicate.get("evidence")
        if (
            not isinstance(source, str) or source not in known or source in duplicate_ids
            or source in cited or source in delegated
            or not isinstance(covered, list) or not covered
            or any(not isinstance(target, str) or target == source
                   or target not in known or target not in cited for target in covered)
            or not isinstance(evidence, str) or not evidence.strip()
        ):
            fail("Duplicate sources must explicitly map all their content to examined sources.")
        duplicate_ids.add(source)
    unaccounted = 0
    for prior in prior_items:
        if prior["id"] in cited | delegated | duplicate_ids:
            continue
        if prior.get("source_type", "legacy") == "legacy":
            fail(
                f"Earlier source {prior['id']} is unexamined. Account for every point "
                "or explicitly map the whole source to examined duplicates; its body "
                "may mix code feedback and superseded CI results."
            )
        recovered = synthesize(prior)
        if recovered is not None:
            normalized.append(recovered)
            unaccounted += 1

    outstanding = [item for item in normalized if item["status"] in OUTSTANDING]
    resolved = [item for item in normalized if item["status"] in CLOSED]
    confirmations = confirmations_for(resolved, outstanding, by_id)
    for item in outstanding:
        thread = thread_of(item, by_id)
        if thread:
            item["thread_id"] = thread["thread_id"]
            item["comment_id"] = thread["comment_id"]
            item["thread_resolved"] = bool(thread.get("thread_resolved"))
    outstanding.extend(new_items)

    for entry in ci_entries:
        if (
            not isinstance(entry, dict) or entry.get("severity") != BLOCKING
            or any(not isinstance(entry.get(key), str) for key in ("title", "body", "suggestion"))
        ):
            fail("CI findings contain a malformed blocking finding.")
        item, _ = normalize_ci_finding(entry)
        if item is None:
            fail("CI finding could not be read.")
        outstanding.append({
            **item, "status": "ci", "source_type": "ci", "source_id": "", "source_ids": []
        })

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
            "questions": [
                ci_question_for(f) if f["status"] == "ci" else question_for(f)
                for f in findings
            ],
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
            "ci_count": len(ci_entries),
            "delegated_ci_count": len(delegated),
            "duplicate_source_count": len(duplicate_ids),
            # An entry citing no prior comment, or one that does not exist, is
            # a point the analysis introduced rather than followed up. It is
            # still asked — the human drops what does not belong — but the
            # count says it happened.
            "new_count": len(new_items),
            "confirmations": confirmations,
            "unsourced_count": sum(
                1
                for item in normalized
                if known and (not item["source_ids"] or not set(item["source_ids"]) <= known)
            ),
        }
    )


if __name__ == "__main__":
    main()
