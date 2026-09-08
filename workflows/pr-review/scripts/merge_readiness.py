#!/usr/bin/env python3
"""Inspect fresh merge eligibility without changing the pull request."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from urllib.parse import quote

COMMAND_TIMEOUT = 30
PR_FIELDS = "headRefOid,baseRefName,state,isDraft,mergeable,mergeStateStatus,reviewDecision,url"


class QueryError(RuntimeError):
    pass


def validate_target(nwo: str, pr_number: str, head_sha: str) -> None:
    if (
        not isinstance(nwo, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.][A-Za-z0-9_.-]*", nwo)
        or nwo.split("/")[-1] in {".", ".."}
    ):
        raise QueryError("Expected an owner/repository name.")
    if not str(pr_number).isdigit() or int(pr_number) < 1:
        raise QueryError("Expected a positive pull request number.")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        raise QueryError("Expected the full reviewed head commit SHA.")


def command(args: list[str], deadline: float | None = None) -> subprocess.CompletedProcess:
    timeout = COMMAND_TIMEOUT
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise QueryError("CI verification timed out.")
    try:
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QueryError(f"gh {' '.join(args[:3])} could not complete: {exc}") from exc


def parse_json(text: str, label: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise QueryError(f"{label} returned invalid JSON: {exc}") from exc


def query(args: list[str], deadline: float | None = None):
    proc = command(args, deadline)
    if proc.returncode:
        raise QueryError(f"gh {' '.join(args[:3])}: {proc.stderr.strip() or proc.stdout.strip() or 'request failed'}")
    value = parse_json(proc.stdout, "gh")
    if isinstance(value, dict) and value.get("errors"):
        raise QueryError(f"GitHub query errors: {value['errors']}")
    return value


def paginated(endpoint: str, key: str | None = None, deadline: float | None = None) -> list:
    pages = query(["api", endpoint, "--paginate", "--slurp"], deadline)
    if not isinstance(pages, list):
        raise QueryError(f"{endpoint}: expected paginated JSON arrays.")
    result = []
    for page in pages:
        entries = page.get(key) if key and isinstance(page, dict) else page
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise QueryError(f"{endpoint}: malformed page.")
        result.extend(entries)
    return result


def required_contexts(nwo: str, pr_number: str, base: str, deadline=None) -> set[str]:
    owner, name = nwo.split("/")
    data = query([
        "api", "graphql", "-f",
        "query=query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){pullRequest(number:$number){"
        "baseRef{branchProtectionRule{requiresStatusChecks requiredStatusCheckContexts}}}}}",
        "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={pr_number}",
    ], deadline)
    try:
        base_ref = data["data"]["repository"]["pullRequest"]["baseRef"]
        protection = base_ref["branchProtectionRule"]
    except (KeyError, TypeError) as exc:
        raise QueryError("Could not determine classic branch protection.") from exc
    contexts = set()
    if protection is not None:
        if not isinstance(protection, dict) or not isinstance(protection.get("requiresStatusChecks"), bool):
            raise QueryError("Malformed branch protection response.")
        if protection["requiresStatusChecks"]:
            names = protection.get("requiredStatusCheckContexts")
            if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
                raise QueryError("Malformed required status contexts.")
            contexts.update(names)
    rules = paginated(f"repos/{nwo}/rules/branches/{quote(base, safe='')}?per_page=100", deadline=deadline)
    for rule in rules:
        if rule.get("type") == "required_status_checks":
            parameters = rule.get("parameters")
            checks = parameters.get("required_status_checks") if isinstance(parameters, dict) else None
            if not isinstance(checks, list):
                raise QueryError("Malformed ruleset required checks.")
            for check in checks:
                if not isinstance(check, dict) or not isinstance(check.get("context"), str) or not check["context"]:
                    raise QueryError("Malformed ruleset required check context.")
                contexts.add(check["context"])
    return contexts


def check_rows(nwo: str, pr_number: str, required: bool, deadline=None) -> list[dict]:
    args = ["pr", "checks", str(pr_number), "-R", nwo, "--json", "name,state,bucket,link,workflow"]
    if required:
        args.append("--required")
    proc = command(args, deadline)
    text = proc.stdout.strip()
    if not text and proc.returncode == 1:
        message = proc.stderr.strip().lower()
        if re.fullmatch(r"no checks reported on the '[^\r\n]+' branch", message) or (
            required and re.fullmatch(r"no required checks reported on the '[^\r\n]+' branch", message)
        ):
            return []
    if proc.returncode not in (0, 1, 8) or not text:
        raise QueryError(f"Could not retrieve {'required ' if required else ''}PR checks: {proc.stderr.strip() or text}")
    rows = parse_json(text, "gh pr checks")
    if not isinstance(rows, list):
        raise QueryError("gh pr checks returned a non-array.")
    for row in rows:
        if not isinstance(row, dict) or any(
            not isinstance(row.get(key), str) or not row[key]
            for key in ("name", "state", "bucket")
        ):
            raise QueryError("gh pr checks returned an incomplete check.")
    return rows


def inspect_checks(nwo: str, pr_number: str, base: str, deadline=None) -> dict:
    """Return failing and pending checks; raise QueryError if their configuration is unknown."""
    configured = required_contexts(nwo, pr_number, base, deadline)
    rows = check_rows(nwo, pr_number, False, deadline)
    required = check_rows(nwo, pr_number, True, deadline)
    issues, pending = [], []
    required_names = configured | {row["name"] for row in required}
    names = {row["name"] for row in rows}
    for name in sorted(required_names - names):
        pending.append(f"Required check {name!r} is missing (no result reported).")
    seen = set()
    for row in [*rows, *required]:
        label = f"{row.get('workflow') or 'Check'} / {row['name']}: {row['state']}"
        if row.get("link"):
            label += f" — {row['link']}"
        if label in seen:
            continue
        seen.add(label)
        bucket = row["bucket"]
        state = str(row["state"]).upper()
        if bucket == "pending" or state in {"EXPECTED", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}:
            pending.append(label)
        elif state in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}:
            issues.append(label)
        elif bucket not in {"pass", "skipping"} or state not in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            issues.append(f"Failed or unknown check: {label}")
    return {
        "issues": issues, "pending": pending, "checks": rows,
        "required_names": sorted(required_names),
        "no_checks_configured": not required_names,
    }


def inspect_readiness(nwo: str, pr_number: str, head_sha: str) -> dict:
    """Return fail-closed eligibility and human-readable issues for the exact reviewed head."""
    result = {"ok": False, "error": "", "can_merge": False, "issues": [], "summary": ""}
    issues = result["issues"]
    try:
        validate_target(nwo, pr_number, head_sha)
        pr = query(["pr", "view", str(pr_number), "-R", nwo, "--json", PR_FIELDS])
        if not isinstance(pr, dict) or any(field not in pr for field in PR_FIELDS.split(",")):
            raise QueryError("Incomplete pull request readiness response.")
        if pr["headRefOid"] != head_sha:
            issues.append(f"Reviewed head is stale: reviewed {head_sha}, current {pr['headRefOid']}. Review the current head.")
        if pr["state"] != "OPEN":
            issues.append(f"Pull request is not open (state: {pr['state']}).")
        if pr["isDraft"] is not False:
            issues.append("Pull request is a draft or draft status is unknown.")
        if pr["mergeable"] != "MERGEABLE":
            issues.append(f"Mergeability is {pr['mergeable'] or 'unknown'}; resolve conflicts or wait for GitHub to calculate it.")
        if pr["reviewDecision"] not in ("APPROVED", ""):
            issues.append(f"Review requirement is not satisfied: {pr['reviewDecision'] or 'unknown'}.")
        if pr["mergeStateStatus"] not in ("CLEAN", "HAS_HOOKS"):
            issues.append(f"GitHub merge state is {pr['mergeStateStatus'] or 'unknown'} (required checks, reviews, branch updates or repository rules may block merging).")
        if not isinstance(pr["baseRefName"], str) or not pr["baseRefName"]:
            raise QueryError("Pull request base branch is unknown.")
        checks = inspect_checks(nwo, pr_number, pr["baseRefName"])
        issues.extend(checks["issues"])
        issues.extend(checks["pending"])
        result["no_checks_configured"] = checks["no_checks_configured"]
        after = query(["pr", "view", str(pr_number), "-R", nwo, "--json", "headRefOid"])
        if not isinstance(after, dict) or after.get("headRefOid") != head_sha:
            issues.append("Pull request head changed during readiness inspection; review the current head.")
        result["ok"] = True
        result["can_merge"] = not issues
        suffix = f" {pr['url']}" if pr["url"] else ""
        result["summary"] = (
            ("Ready to merge the reviewed head." if not issues else "Merge blocked: " + "; ".join(issues))
            + (" No required checks are configured." if checks["no_checks_configured"] else "") + suffix
        )
    except QueryError as exc:
        result["error"] = str(exc)
        issues.append(f"Readiness could not be verified: {exc}")
        result["summary"] = "; ".join(issues)
    return result


def main(argv: list[str]) -> None:
    if len(argv) != 3:
        payload = {
            "ok": False, "error": "Usage: merge_readiness.py <owner/repo> <pr_number> <reviewed_head_sha>",
            "can_merge": False, "issues": ["Missing merge-readiness arguments."],
            "summary": "Merge readiness could not be verified.",
        }
    else:
        payload = inspect_readiness(*argv)
    print(json.dumps(payload))


if __name__ == "__main__":
    main(sys.argv[1:])
