"""Read and act on pull request review threads, which only GraphQL exposes."""

from __future__ import annotations

import json
import subprocess

THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 100) { nodes { databaseId } }
          latest: comments(last: 1) { nodes { author { login } } }
        }
      }
    }
  }
}
"""

RESOLVE = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"
UNRESOLVE = "mutation($id: ID!) { unresolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"


def run(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(args, input=stdin, capture_output=True, text=True)  # noqa: S603
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def graphql(query: str, variables: dict[str, object]) -> tuple[dict | None, str]:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    rc, out, err = run(args)
    if rc != 0:
        return None, err or out or "gh api graphql returned a non-zero exit code"
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        return None, f"GraphQL response is not JSON: {exc}"
    if isinstance(parsed, dict) and parsed.get("errors"):
        return None, json.dumps(parsed["errors"])[:400]
    return parsed, ""


def fetch_threads(nwo: str, pr_number: str) -> tuple[dict[int, dict[str, object]], str]:
    """Map every review comment's database id to its thread.

    Each entry carries the thread node id, whether it is resolved, the id of
    the top-level comment a reply must target, and the login that spoke last.
    """
    owner, _, name = nwo.partition("/")
    by_comment: dict[int, dict[str, object]] = {}
    cursor: str | None = None
    while True:
        data, error = graphql(
            THREADS_QUERY,
            {"owner": owner, "name": name, "number": int(pr_number), "cursor": cursor},
        )
        if data is None:
            return {}, error
        try:
            connection = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            for node in connection["nodes"]:
                ids = [c["databaseId"] for c in node["comments"]["nodes"] if c.get("databaseId")]
                latest = node["latest"]["nodes"]
                author = ((latest[0].get("author") or {}).get("login") or "") if latest else ""
                for comment_id in ids:
                    by_comment[comment_id] = {
                        "thread_id": node["id"],
                        "resolved": bool(node["isResolved"]),
                        "root_comment_id": ids[0],
                        "last_author": author,
                    }
            if not connection["pageInfo"]["hasNextPage"]:
                return by_comment, ""
            cursor = connection["pageInfo"]["endCursor"]
        except (KeyError, TypeError, IndexError):
            return {}, "Unexpected review thread response shape."


def valid_action(action: object) -> bool:
    return (
        isinstance(action, dict)
        and isinstance(action.get("thread_id"), str) and action["thread_id"]
        and type(action.get("comment_id")) is int
        and isinstance(action.get("body"), str)
        and action.get("resolve") in ("resolve", "unresolve", "")
    )


def apply_actions(nwo: str, pr_number: str, actions: list[dict[str, object]]) -> tuple[int, list[str]]:
    """Reply on each thread, then resolve or reopen it; keep going past failures."""
    done, errors = 0, []
    for action in actions:
        label = f"thread {action['thread_id']}"
        if action["body"].strip():
            rc, out, err = run(
                ["gh", "api", "--method", "POST",
                 f"repos/{nwo}/pulls/{pr_number}/comments/{action['comment_id']}/replies",
                 "--input", "-"],
                stdin=json.dumps({"body": action["body"]}),
            )
            if rc != 0:
                errors.append(f"Could not reply on {label}: {err or out}")
                continue
        if action["resolve"]:
            query = RESOLVE if action["resolve"] == "resolve" else UNRESOLVE
            _, error = graphql(query, {"id": action["thread_id"]})
            if error:
                errors.append(f"Replied but could not {action['resolve']} {label}: {error}")
                continue
        done += 1
    return done, errors
