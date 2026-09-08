#!/usr/bin/env python3
"""Collect everything this reviewer has already said on a pull request.

A second visit to a pull request is not a second first review. Running the
concept and code passes again re-derives points the author has already read,
already argued with, and in most cases already fixed — and posts them a second
time under a fresh review, which reads as if nobody looked at the replies.

So the workflow branches here, and the branch has to be decided by a script
rather than by asking a model whether it recognises its own handiwork. What
counts as "I reviewed this" is a fact GitHub can answer: a submitted review, an
inline comment, or a conversation comment, authored by any login `gh` holds a
token for on this host.

Identity is a set, not a name. One human routinely has more than one GitHub
account — work and personal, or a tenant migration halfway done — and `gh`
keeps them all, active one at a time. Matching only the active login means a
review you left as one account is invisible when you come back signed in as
another: `has_prior_review` comes back false, the workflow takes the first-pass
branch, and posts a fresh review over the top of your own. So every login you
are signed in as counts as you, and `matched_logins` reports which of them
actually spoke here — the follow-up posts as the active account, and the human
should be told when that is not the account they reviewed under.

Three endpoints are needed because GitHub files those three in three places:

    pulls/N/reviews     the review summaries, with the commit each was
                        submitted against — which is what makes
                        `git diff <commit>...HEAD` mean "since I looked"
    pulls/N/comments    every inline comment on the diff, mine and the
                        replies to mine
    issues/N/comments   the conversation tab

Replies are attached to the comment they answer, because "the author said they
fixed it" is the single most useful piece of context for deciding whether they
did, and it is worthless if the analysis step has to guess which point a reply
was answering.

`has_prior_review` is deliberately derived from the *items*, not the counts. An
approval with an empty body leaves nothing to verify, and routing that into a
follow-up pass would produce an analysis of nothing.

Usage:
    prior_review.py <name_with_owner> <pr_number> <gh_user> [other_logins] [head_sha] [worktree]

    `other_logins` is a comma-separated list of the remaining logins `gh` is
    authenticated as on this host. `gh_user` is the account that will post.

Output:
    ok, error, has_prior_review, prior_items, context_comments, review_count,
    inline_count, issue_comment_count, reply_count, items_dropped,
    matched_logins, other_login_items, last_interaction_at, last_review_at,
    last_review_state, last_review_commit, comparison_commit, change_status, change_summary
"""

from __future__ import annotations

import json
import subprocess
import sys

BODY_LIMIT = 6000
REPLY_LIMIT = 1500
CONTEXT_LIMIT = 1000

# The newest items are kept when a thread runs long: an old point is the one
# most likely to have been settled already, and the analysis step reads every
# item it is given.
MAX_ITEMS = 80
MAX_REPLIES = 5
MAX_CONTEXT = 12

TRUNCATION_MARK = "\n\n[…truncated]"


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload))
    sys.exit(0)


def fail(message: str) -> None:
    emit(
        {
            "ok": False,
            "error": message,
            "has_prior_review": False,
            "change_status": "unknown",
            "change_summary": message,
            "comparison_commit": "",
            "prior_items": [],
            "context_comments": [],
            "review_count": 0,
            "inline_count": 0,
            "issue_comment_count": 0,
            "reply_count": 0,
            "items_dropped": 0,
            "matched_logins": [],
            "other_login_items": 0,
            "last_interaction_at": "",
            "last_review_at": "",
            "last_review_state": "",
            "last_review_commit": "",
        }
    )


def run(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(args, capture_output=True, text=True)  # noqa: S603
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def truncate(value: object, limit: int) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + TRUNCATION_MARK


def fetch(endpoint: str) -> tuple[list[dict[str, object]] | None, str]:
    """Read every page of an array endpoint.

    `--jq '.[]'` is what makes `--paginate` usable here: without it gh
    concatenates one JSON array per page, which is not a JSON document. With
    it, each element arrives compact on its own line, newlines inside bodies
    escaped, so the pages parse as JSON Lines regardless of how many there
    were.
    """
    rc, out, err = run(["gh", "api", "--paginate", endpoint, "--jq", ".[]"])
    if rc != 0:
        return None, err or out or f"'gh api {endpoint}' returned a non-zero exit code"
    entries: list[dict[str, object]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            return None, f"'gh api {endpoint}' returned a line that is not JSON: {exc}"
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries, ""


def login_of(entry: dict[str, object]) -> str:
    user = entry.get("user")
    if not isinstance(user, dict):
        return ""
    return str(user.get("login") or "")


def stamp_of(entry: dict[str, object]) -> str:
    """The ISO-8601 timestamp GitHub filed this under.

    Reviews carry `submitted_at` and comments carry `created_at`. Both are UTC
    and zero-padded, so string comparison orders them correctly.
    """
    return str(entry.get("submitted_at") or entry.get("created_at") or "")


def thread_root(comment: dict[str, object]) -> object:
    """The id of the comment a thread hangs off, which is its own when it is the root."""
    return comment.get("in_reply_to_id") or comment.get("id")


def line_of(comment: dict[str, object]) -> int:
    """The line an inline comment is anchored to, preferring where it sits now.

    `line` is null once the comment is outdated by a later push, and
    `original_line` is where it was made. Either is a useful pointer for a
    human reading the item back.
    """
    for key in ("line", "original_line"):
        try:
            value = int(str(comment.get(key)).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def changes_since_review(
    previous_sha: str, head_sha: str, worktree: str, has_history: bool
) -> tuple[str, str]:
    """Compare reviewed content; unavailable history is unknown, not unchanged."""
    if not has_history:
        return "no_previous_review", ""
    if not previous_sha or not head_sha:
        return "unknown", "The earlier interaction has no comparable review commit."
    if previous_sha == head_sha:
        return "unchanged", f"The pull request is still at the reviewed commit {head_sha}."
    if not worktree:
        return "unknown", "No worktree was provided to compare the review commits."
    rc, out, err = run(
        ["git", "-C", worktree, "diff", "--quiet", previous_sha, head_sha, "--"]
    )
    if rc == 0:
        return "unchanged", "The commit changed, but the file contents match the previous review."
    if rc == 1:
        return "changed", "The file contents changed since the previous review."
    return "unknown", f"Could not compare the previous review commit: {err or out}"


def replies_to(
    comment: dict[str, object],
    threads: dict[object, list[dict[str, object]]],
    mine: set[str],
) -> list[dict[str, str]]:
    """Everything anyone else said in this thread after the given comment."""
    said_at = stamp_of(comment)
    replies: list[dict[str, str]] = []
    for other in threads.get(thread_root(comment), []):
        if login_of(other).casefold() in mine:
            continue
        if stamp_of(other) <= said_at:
            continue
        body = truncate(other.get("body"), REPLY_LIMIT)
        if not body:
            continue
        replies.append(
            {
                "author": login_of(other),
                "created_at": stamp_of(other),
                "body": body,
            }
        )
    replies.sort(key=lambda reply: reply["created_at"])
    # The newest replies are the ones kept. A long thread ends in its
    # resolution, and dropping the tail to keep the opening exchange discards
    # exactly the part that says whether the point was settled.
    return replies[-MAX_REPLIES:]


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        fail("prior_review.py requires name_with_owner, pr_number and gh_user")

    nwo, pr_number, gh_user = argv[0], argv[1], argv[2].strip()
    if not pr_number.isdigit():
        fail(f"Pull request number is not numeric: {pr_number!r}")
    if not gh_user:
        fail(
            "No GitHub login was provided, so there is no way to tell your "
            "earlier comments from anyone else's."
        )

    others = argv[3] if len(argv) > 3 else ""
    logins = [gh_user, *(part.strip() for part in others.split(",") if part.strip())]
    mine = {login.casefold() for login in logins}

    reviews, error = fetch(f"repos/{nwo}/pulls/{pr_number}/reviews")
    if reviews is None:
        fail(f"Could not read the reviews on {nwo}#{pr_number}. {error}")
    inline, error = fetch(f"repos/{nwo}/pulls/{pr_number}/comments")
    if inline is None:
        fail(f"Could not read the review comments on {nwo}#{pr_number}. {error}")
    conversation, error = fetch(f"repos/{nwo}/issues/{pr_number}/comments")
    if conversation is None:
        fail(f"Could not read the conversation comments on {nwo}#{pr_number}. {error}")

    me = gh_user.casefold()
    # A PENDING review is a draft only its author can see. Treating one as a
    # prior review would branch the workflow on something the pull request's
    # author has never been shown — and `pulls/N/comments` hands its inline
    # comments back to you, so the drafts have to be excluded by review id too.
    my_reviews = []
    draft_reviews: set[object] = set()
    for review in reviews:
        if login_of(review).casefold() not in mine:
            continue
        if str(review.get("state") or "").upper() == "PENDING":
            if review.get("id") is not None:
                draft_reviews.add(review["id"])
        else:
            my_reviews.append(review)

    my_inline = [
        comment
        for comment in inline
        if login_of(comment).casefold() in mine
        and comment.get("pull_request_review_id") not in draft_reviews
    ]
    my_conversation = [c for c in conversation if login_of(c).casefold() in mine]

    threads: dict[object, list[dict[str, object]]] = {}
    for comment in inline:
        threads.setdefault(thread_root(comment), []).append(comment)

    items: list[dict[str, object]] = []
    for review in my_reviews:
        body = truncate(review.get("body"), BODY_LIMIT)
        if not body:
            continue
        items.append(
            {
                "kind": "review",
                "author": login_of(review),
                "path": "",
                "line": 0,
                "created_at": stamp_of(review),
                "state": str(review.get("state") or ""),
                "url": str(review.get("html_url") or ""),
                "body": body,
                "replies": [],
            }
        )
    for comment in my_inline:
        body = truncate(comment.get("body"), BODY_LIMIT)
        if not body:
            continue
        items.append(
            {
                "kind": "inline",
                "author": login_of(comment),
                "path": str(comment.get("path") or ""),
                "line": line_of(comment),
                "created_at": stamp_of(comment),
                "state": "",
                "url": str(comment.get("html_url") or ""),
                "body": body,
                "replies": replies_to(comment, threads, mine),
            }
        )
    for comment in my_conversation:
        body = truncate(comment.get("body"), BODY_LIMIT)
        if not body:
            continue
        items.append(
            {
                "kind": "conversation",
                "author": login_of(comment),
                "path": "",
                "line": 0,
                "created_at": stamp_of(comment),
                "state": "",
                "url": str(comment.get("html_url") or ""),
                "body": body,
                "replies": [],
            }
        )

    items.sort(key=lambda item: str(item["created_at"]))
    dropped = max(0, len(items) - MAX_ITEMS)
    items = items[-MAX_ITEMS:]
    for index, item in enumerate(items, 1):
        item["id"] = f"p{index}"

    stamps = [
        stamp_of(entry)
        for entry in (*my_reviews, *my_inline, *my_conversation)
        if stamp_of(entry)
    ]
    last_interaction_at = max(stamps) if stamps else ""
    latest_review = max(my_reviews, key=stamp_of) if my_reviews else None
    previous_sha = str(latest_review.get("commit_id") or "") if latest_review else ""
    if not latest_review and my_inline:
        latest_inline = max(my_inline, key=stamp_of)
        previous_sha = str(
            latest_inline.get("original_commit_id") or latest_inline.get("commit_id") or ""
        )
    change_status, change_summary = changes_since_review(
        previous_sha,
        argv[4] if len(argv) > 4 else "",
        argv[5] if len(argv) > 5 else "",
        bool(my_reviews or my_inline or my_conversation),
    )

    context = [
        {
            "author": login_of(comment),
            "created_at": stamp_of(comment),
            "body": truncate(comment.get("body"), CONTEXT_LIMIT),
        }
        for comment in conversation
        if login_of(comment).casefold() not in mine
        and (not last_interaction_at or stamp_of(comment) > last_interaction_at)
    ]
    context = [entry for entry in context if entry["body"]][-MAX_CONTEXT:]

    # Derived from the items rather than the raw matches, so it describes
    # exactly the account the follow-up step is about to read back.
    matched_logins = sorted({str(item["author"]) for item in items if item["author"]})
    other_login_items = sum(1 for item in items if str(item["author"]).casefold() != me)

    emit(
        {
            "ok": True,
            "error": "",
            "has_prior_review": bool(items),
            "change_status": change_status,
            "change_summary": change_summary,
            "comparison_commit": previous_sha,
            "prior_items": items,
            "context_comments": context,
            "review_count": len(my_reviews),
            "inline_count": len(my_inline),
            "issue_comment_count": len(my_conversation),
            "reply_count": sum(len(item["replies"]) for item in items),
            "items_dropped": dropped,
            "matched_logins": matched_logins,
            # Anything you said here under a login that is not the one now
            # active. The follow-up posts as the active account, so a run that
            # reads back another account's review would split one reviewer
            # across two names on the pull request without saying so.
            "other_login_items": other_login_items,
            "last_interaction_at": last_interaction_at,
            # Kept separate from `last_interaction_at`, and reported as a set
            # with the state and commit it belongs to. A conversation comment
            # posted after the review moves the interaction timestamp, and
            # pairing that later time with this older commit would describe a
            # diff as "what changed since" a moment it does not reach back to.
            "last_review_at": stamp_of(latest_review) if latest_review else "",
            "last_review_state": str(latest_review.get("state") or "") if latest_review else "",
            "last_review_commit": str(latest_review.get("commit_id") or "") if latest_review else "",
        }
    )


if __name__ == "__main__":
    main(sys.argv[1:])
