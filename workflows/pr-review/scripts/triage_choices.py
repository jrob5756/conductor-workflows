#!/usr/bin/env python3
"""The vocabulary the triage steps share.

Three scripts have to agree on these strings exactly. `build_questions.py`
offers them as choices on a first-pass review, `build_followup_questions.py`
offers the same ones when re-raising points from an earlier review, and
`apply_triage.py` decides what each answer meant. Answers arrive as the choice
text verbatim, so a string that drifts in one file silently stops meaning what
the human was told it meant — there is no error, just a decision applied as
something else.

The coercion helpers live here for the same reason: both builders normalize a
model's severity label, and two implementations of "is this blocking?" is one
too many.

This module is imported, never run.
"""

from __future__ import annotations

POST_AS_IS = "Post as-is"
DO_NOT_POST = "Do not post"
POST_AS_BLOCKING = "Post as BLOCKING"
POST_AS_RECOMMENDED = "Post as RECOMMENDED"

BLOCKING = "BLOCKING"
RECOMMENDED = "RECOMMENDED"

# Only the opposite severity is ever offered. A choice that restates the label
# a finding already carries is a no-op the human has to read past on every
# question, and it makes "did I change this one?" unanswerable from the answer.
FLIP_CHOICE = {BLOCKING: POST_AS_RECOMMENDED, RECOMMENDED: POST_AS_BLOCKING}

# Matching is case-insensitive so a human who typed the words instead of
# selecting them still gets what they asked for.
RESEVERITY = {
    POST_AS_BLOCKING.casefold(): BLOCKING,
    POST_AS_RECOMMENDED.casefold(): RECOMMENDED,
}

# A reviewer told to emit BLOCKING or RECOMMENDED still reaches for its own
# vocabulary sometimes. Mapping the escalating words onto BLOCKING matters far
# more than the reverse: silently filing a CRITICAL finding as a suggestion is
# the one normalization error that loses real severity.
ESCALATING = ("block", "critical", "severe", "major", "high", "must", "error", "bug")
DISCARDED = ("nit", "trivial", "cosmetic", "style")

TITLE_LIMIT = 160


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
    """Normalize a severity label, returning an empty string for a nit."""
    label = text_of(value).lower()
    if any(label.startswith(word) for word in DISCARDED):
        return ""
    if any(word in label for word in ESCALATING):
        return BLOCKING
    return RECOMMENDED
