#!/usr/bin/env python3
"""Start PR-associated Actions runs and wait for CI on a pinned reviewed head."""

from __future__ import annotations

import json
import re
import sys
import time
from urllib.parse import parse_qs, urlsplit

from merge_readiness import (
    QueryError, command, inspect_checks, paginated, parse_json, query, validate_target,
)

WAIT_TIMEOUT = 1800
START_TIMEOUT = 240
POLL_INTERVAL = 15
DISCOVERY_GRACE = 120
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
PASSING = {"success", "neutral", "skipped"}


def finding(title: str, body: str) -> dict:
    return {
        "severity": "BLOCKING", "title": title, "body": body, "source_type": "ci",
        "suggestion": "Resolve the CI problem on the reviewed commit, then rerun the review and verify checks before merging.",
    }


def response(findings=None, **extra) -> dict:
    findings = [{**item, "source_type": "ci"} for item in findings or []]
    return {
        "ok": True, "error": "", "findings": findings,
        "summary": "; ".join(f"{item['title']}: {item['body']}" for item in findings) if findings else "CI verified.",
        **extra,
    }


def fatal(error: str, findings=None, **extra) -> dict:
    findings = list(findings or [])
    diagnostic = finding("CI verification could not complete", error)
    if diagnostic not in findings:
        findings.append(diagnostic)
    return response(findings, **{**extra, "ok": False, "error": error})


def read_pr(nwo: str, pr_number: str, head_sha: str, deadline=None) -> dict:
    pr = query(["api", f"repos/{nwo}/pulls/{pr_number}"], deadline)
    try:
        current = pr["head"]["sha"]
        base = pr["base"]["ref"]
    except (KeyError, TypeError) as exc:
        raise QueryError("Incomplete pull request head/base response.") from exc
    if current != head_sha:
        raise QueryError(f"Reviewed head is stale: reviewed {head_sha}, current {current}.")
    if not isinstance(base, str) or not base:
        raise QueryError("Pull request base branch is unknown.")
    if pr.get("state") != "open":
        raise QueryError(f"Pull request is not open: {pr.get('state', 'unknown')}.")
    return pr


def validate_run(run: dict) -> None:
    for key in ("id", "workflow_id", "run_attempt"):
        if type(run.get(key)) is not int or run[key] < 1:
            raise QueryError(f"Actions run has invalid {key}: {run.get(key)!r}.")
    if not isinstance(run.get("status"), str) or run["status"] not in ACTIVE | {"completed"}:
        raise QueryError(f"Actions run {run['id']} has unknown status: {run.get('status')!r}.")
    if run["status"] == "completed" and (
        not isinstance(run.get("conclusion"), str) or not run["conclusion"]
    ):
        raise QueryError(f"Actions run {run['id']} is completed without a conclusion.")


def execution_identity(run: dict) -> tuple:
    repository = run.get("head_repository") or {}
    if (
        not isinstance(repository, dict) or not isinstance(repository.get("full_name", ""), str)
        or not isinstance(run.get("head_branch", ""), str)
        or run.get("event") not in ("push", "pull_request")
    ):
        raise QueryError(f"Actions run {run.get('id')} has malformed execution metadata.")
    return (
        run["workflow_id"], run["event"], run["head_sha"],
        repository.get("full_name", ""), run.get("head_branch", ""),
    )


def matches_pr_ref(nwo: str, pr_number: str, head_sha: str, pr: dict, run: dict) -> bool:
    head = pr.get("head") or {}
    repository = head.get("repo") or {}
    run_repository = run.get("head_repository") or {}
    branch = run.get("head_branch")
    if (
        not isinstance(branch, str) or not branch
        or not isinstance(repository, dict) or not isinstance(run_repository, dict)
    ):
        return False
    if (
        repository.get("full_name")
        and run_repository.get("full_name") == repository["full_name"]
        and branch == head.get("ref")
    ):
        return run["event"] == "pull_request" or run["head_sha"] == head_sha
    return (
        run["event"] == "pull_request"
        and run["head_sha"] == pr.get("merge_commit_sha")
        and run_repository.get("full_name") == nwo
        and branch == f"refs/pull/{pr_number}/merge"
    )


def discover(nwo: str, pr_number: str, head_sha: str, pr: dict, deadline=None) -> list[dict]:
    shas = {head_sha}
    if pr.get("merge_commit_sha"):
        validate_target(nwo, pr_number, pr["merge_commit_sha"])
        shas.add(pr["merge_commit_sha"])
    associated = {}
    workflows = {}
    for sha in sorted(shas):
        runs = paginated(f"repos/{nwo}/actions/runs?head_sha={sha}&per_page=100", "workflow_runs", deadline)
        for run in runs:
            if run.get("head_sha") != sha or run.get("event") not in ("pull_request", "push"):
                continue
            prs = run.get("pull_requests")
            if not isinstance(prs, list) or any(not isinstance(item, dict) for item in prs):
                raise QueryError("Actions run has malformed pull request associations.")
            if prs:
                matches = any(
                    item.get("number") == int(pr_number)
                    and isinstance(item.get("head"), dict)
                    and item["head"].get("sha") == head_sha
                    for item in prs
                )
            else:
                if sha not in associated:
                    associated[sha] = paginated(f"repos/{nwo}/commits/{sha}/pulls?per_page=100", deadline=deadline)
                matches = any(
                    item.get("number") == int(pr_number)
                    and isinstance(item.get("head"), dict)
                    and item["head"].get("sha") == head_sha
                    for item in associated[sha]
                )
                # Fork approval runs can omit both run-level and commit-level PR associations.
                if not associated[sha] and run["event"] == "pull_request":
                    matches = matches_pr_ref(nwo, pr_number, head_sha, pr, run)
            if not matches:
                continue
            if (not prs or run["event"] == "push") and not matches_pr_ref(nwo, pr_number, head_sha, pr, run):
                raise QueryError(f"Actions run {run.get('id')} has no verified PR head repository/ref association.")
            validate_run(run)
            workflow = execution_identity(run)
            if workflow not in workflows or (run["id"], run["run_attempt"]) > (
                workflows[workflow]["id"], workflows[workflow]["run_attempt"],
            ):
                workflows[workflow] = run
    return sorted(workflows.values(), key=lambda item: item["id"])


def run_label(nwo: str, run: dict) -> str:
    url = run.get("html_url") or f"https://github.com/{nwo}/actions/runs/{run['id']}"
    return (
        f"{run.get('name') or 'Actions run'} #{run['id']} attempt {run['run_attempt']}: "
        f"{run['status']}/{run.get('conclusion') or 'pending'} — "
        f"{url}"
    )


def tracking(run: dict, action: str = "existing") -> dict:
    return {
        "id": run["id"], "workflow_id": run["workflow_id"], "head_sha": run["head_sha"],
        "expected_attempt": run["run_attempt"] + (1 if action == "rerun" else 0),
        "action": action, "execution_identity": list(execution_identity(run)),
        "reason": (
            "retried_failed" if action == "rerun" else
            "approved_fork" if action == "approve" else
            "reused_passing" if run["status"] == "completed" and run.get("conclusion") in PASSING
            else "already_running" if run["status"] in ACTIVE else "discovered"
        ),
    }


def start(nwo: str, pr_number: str, head_sha: str) -> dict:
    findings, tracked, decisions = [], [], []
    error = ""
    deadline = time.monotonic() + START_TIMEOUT
    try:
        validate_target(nwo, pr_number, head_sha)
        pr = read_pr(nwo, pr_number, head_sha, deadline)
        runs = discover(nwo, pr_number, head_sha, pr, deadline)
        for run in runs:
            action = "existing"
            read_pr(nwo, pr_number, head_sha, deadline)
            try:
                if run.get("conclusion") == "action_required":
                    if run["event"] != "pull_request":
                        raise QueryError("Only pull_request runs can receive fork execution approval.")
                    action, endpoint = "approve", "approve"
                elif run["status"] == "completed" and run.get("conclusion") not in PASSING:
                    action, endpoint = "rerun", "rerun"
                else:
                    tracked.append(tracking(run))
                    decision = "Reused passing" if run["status"] == "completed" else "Already running"
                    decisions.append(f"{decision}: {run_label(nwo, run)}")
                    continue
                proc = command(["api", f"repos/{nwo}/actions/runs/{run['id']}/{endpoint}", "--method", "POST"], deadline)
                if proc.returncode:
                    raise QueryError(proc.stderr.strip() or proc.stdout.strip() or "GitHub rejected the request.")
                tracked.append(tracking(run, action))
                decision = "Approved fork" if action == "approve" else "Retried failed"
                decisions.append(
                    f"{decision}: {run_label(nwo, run)}; "
                    f"awaiting attempt {tracked[-1]['expected_attempt']}"
                )
            except QueryError as exc:
                findings.append(finding(f"Could not {action} PR CI", f"{run_label(nwo, run)}. {exc}"))
        read_pr(nwo, pr_number, head_sha, deadline)
    except QueryError as exc:
        error = str(exc)
    extra = {
        "run_ids": [item["id"] for item in tracked], "runs": tracked,
        "nwo": nwo, "pr_number": str(pr_number), "reviewed_head_sha": head_sha,
        "discovery_pending": not tracked and not error,
    }
    if error:
        return fatal(error, findings, **extra)
    if not tracked:
        decisions.append(
            "No Actions runs tracked yet; discovery is provisional. "
            "Wait will rediscover runs and inspect external/required checks and Actions configuration."
        )
    return response(
        findings, **extra,
        summary="\n".join([
            *decisions, *(f"{item['title']}: {item['body']}" for item in findings),
        ]),
    )


def failed_jobs(nwo: str, run: dict, deadline: float) -> list[dict]:
    jobs = paginated(
        f"repos/{nwo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100",
        "jobs", deadline,
    )
    return [
        job for job in jobs
        if not isinstance(job.get("conclusion"), str) or job["conclusion"] not in PASSING
    ]


def failure_identity(url: str | None) -> tuple | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ("url", url)
    if parsed.hostname == "github.com":
        job = re.fullmatch(r"/([^/]+/[^/]+)/(?:actions/runs/\d+/job|runs)/(\d+)/?", parsed.path)
        if job:
            return ("job", job[1].lower(), job[2])
        workflow = re.fullmatch(r"/([^/]+/[^/]+)/actions/runs/(\d+)/?", parsed.path)
        if workflow:
            return ("run", workflow[1].lower(), workflow[2])
        check = re.fullmatch(r"/([^/]+/[^/]+)/checks", parsed.path)
        check_ids = parse_qs(parsed.query).get("check_run_id", [])
        if check and len(check_ids) == 1 and check_ids[0].isdigit():
            return ("check", check[1].lower(), check_ids[0])
    elif parsed.hostname == "api.github.com":
        check = re.fullmatch(r"/repos/([^/]+/[^/]+)/check-runs/(\d+)", parsed.path)
        if check:
            return ("check", check[1].lower(), check[2])
    return ("url", url)


def enabled_workflows(nwo: str, deadline: float) -> list[dict]:
    workflows = paginated(f"repos/{nwo}/actions/workflows?per_page=100", "workflows", deadline)
    states = {"active", "deleted", "disabled_fork", "disabled_inactivity", "disabled_manually"}
    for workflow in workflows:
        if not isinstance(workflow.get("state"), str) or workflow["state"] not in states:
            raise QueryError("Could not determine enabled Actions workflows: unknown workflow state.")
    return [workflow for workflow in workflows if workflow["state"] == "active"]


def startup_findings(data) -> list[dict]:
    if not isinstance(data, dict):
        raise QueryError("CI start input must be an object.")
    findings = data.get("findings")
    if not isinstance(findings, list) or any(
        not isinstance(item, dict) or item.get("severity") != "BLOCKING"
        or any(not isinstance(item.get(key), str) for key in ("title", "body", "suggestion"))
        for item in findings
    ):
        raise QueryError("CI start input contains malformed findings.")
    return list(findings)


def validate_tracking(data: dict, nwo: str, pr_number: str, head_sha: str) -> list[dict]:
    if not isinstance(data.get("error"), str):
        raise QueryError("CI start input contains a malformed error.")
    if data.get("ok") is not True or data.get("error"):
        raise QueryError(data["error"] or "CI start failed: unknown error.")
    if (data.get("nwo"), str(data.get("pr_number")), data.get("reviewed_head_sha")) != (nwo, str(pr_number), head_sha):
        raise QueryError("CI start tracking does not match this repository, PR and reviewed head.")
    runs = data.get("runs")
    if not isinstance(runs, list):
        raise QueryError("CI start input is missing run attempt tracking.")
    for run in runs:
        if not isinstance(run, dict) or any(
            type(run.get(key)) is not int or run[key] < 1
            for key in ("id", "workflow_id", "expected_attempt")
        ) or not isinstance(run.get("head_sha"), str) or run.get("action") not in ("existing", "rerun", "approve"):
            raise QueryError("CI start input has malformed run attempt tracking.")
        validate_target(nwo, pr_number, run["head_sha"])
        identity = run.get("execution_identity")
        if (
            not isinstance(identity, list) or len(identity) != 5
            or identity[0] != run["workflow_id"] or identity[1] not in ("push", "pull_request")
            or identity[2] != run["head_sha"] or any(not isinstance(part, str) for part in identity[1:])
        ):
            raise QueryError("CI start input has malformed execution identity.")
    if data.get("run_ids") != [run["id"] for run in runs]:
        raise QueryError("CI start run IDs do not match attempt tracking.")
    return list(runs)


def wait(nwo: str, pr_number: str, head_sha: str, data, timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL) -> dict:
    """Wait at most timeout seconds, preserving startup blockers and rerun attempt barriers."""
    findings, failures = [], []
    deadline = time.monotonic() + timeout
    discovery_deadline = min(deadline, time.monotonic() + DISCOVERY_GRACE)
    try:
        findings = startup_findings(data)
        validate_target(nwo, pr_number, head_sha)
        tracked = validate_tracking(data, nwo, pr_number, head_sha)
        while True:
            pr = read_pr(nwo, pr_number, head_sha, deadline)
            current = discover(nwo, pr_number, head_sha, pr, deadline)
            latest = {execution_identity(run): run for run in current}
            tracked = [
                item for item in tracked
                if latest.get(tuple(item["execution_identity"]), {}).get("id", item["id"]) <= item["id"]
            ]
            known = {item["id"] for item in tracked}
            for run in current:
                if run["id"] not in known:
                    tracked.append(tracking(run))
            pending, failures, represented = [], [], set()
            for item in tracked:
                run = query(["api", f"repos/{nwo}/actions/runs/{item['id']}"], deadline)
                if not isinstance(run, dict):
                    raise QueryError(f"Actions run {item['id']} returned a non-object.")
                validate_run(run)
                if (run["id"], run["workflow_id"], run.get("head_sha")) != (item["id"], item["workflow_id"], item["head_sha"]):
                    raise QueryError(f"Actions run {item['id']} no longer matches the tracked workflow and commit.")
                if list(execution_identity(run)) != item["execution_identity"]:
                    raise QueryError(f"Actions run {item['id']} no longer matches the tracked execution identity.")
                label = run_label(nwo, run)
                if run["run_attempt"] < item["expected_attempt"]:
                    pending.append(f"Waiting for requested attempt {item['expected_attempt']}: {label}")
                elif run["status"] in ACTIVE:
                    pending.append(label)
                elif item["action"] == "approve" and run.get("conclusion") == "action_required":
                    pending.append(f"Waiting for fork approval to take effect: {label}")
                elif run.get("conclusion") not in PASSING:
                    failure = finding("PR CI did not pass", label)
                    failures.append(failure)
                    jobs = failed_jobs(nwo, run, deadline)
                    details = [
                        f"{job.get('name') or 'Job'}: {job.get('status', 'unknown')}/"
                        f"{job.get('conclusion') or 'pending'} — {job.get('html_url') or ''}"
                        for job in jobs
                    ]
                    if details:
                        failure["body"] += ". " + "; ".join(details)
                    represented.add(("run", nwo.lower(), str(run["id"])))
                    represented.add(failure_identity(run.get("html_url")))
                    for job in jobs:
                        if type(job.get("id")) is int and job["id"] > 0:
                            represented.add(("job", nwo.lower(), str(job["id"])))
                        represented.update(failure_identity(job.get(key)) for key in ("html_url", "check_run_url"))
            represented.discard(None)
            checks = inspect_checks(nwo, pr_number, pr["base"]["ref"], deadline)
            pending.extend(checks["pending"])
            for issue in checks["issues"]:
                url = issue.rsplit(" — ", 1)[1] if " — " in issue else ""
                identity = failure_identity(url)
                if identity is None or identity not in represented:
                    failures.append(finding("PR check did not pass", issue))
            discovery_pending = False
            if not tracked and enabled_workflows(nwo, deadline):
                message = (
                    "No eligible Actions run is associated with this PR and reviewed head, but enabled "
                    "workflows exist. Their applicability is unknown; verify PR triggers, filters and "
                    "fork policy. Default-branch dispatch does not verify the reviewed head."
                )
                if time.monotonic() < discovery_deadline:
                    discovery_pending = True
                    pending.append("Waiting for Actions discovery: " + message)
                else:
                    failures.append(finding("Actions applicability could not be verified", message))
            read_pr(nwo, pr_number, head_sha, deadline)
            if not pending:
                no_ci = (
                    not tracked and not checks["checks"] and not checks["required_names"]
                    and not failures and not findings
                )
                return response(
                    findings + failures, no_ci=no_ci,
                    **({"summary": "No CI checks apply: no enabled Actions workflows, reported checks or required contexts."}
                       if no_ci and not findings else {}),
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return response(findings + failures + [finding("CI verification timed out", "; ".join(pending))])
            delay = min(poll_interval, discovery_deadline - time.monotonic()) if discovery_pending else poll_interval
            if remaining <= delay:
                # Preserve actionable pending statuses when the next request would exceed the deadline.
                time.sleep(max(0, remaining))
                return response(findings + failures + [finding("CI verification timed out", "; ".join(pending))])
            time.sleep(max(0, delay))
    except QueryError as exc:
        return fatal(str(exc), findings + failures)


def main(argv: list[str]) -> None:
    try:
        if len(argv) != 4 or argv[0] not in {"start", "wait"}:
            raise QueryError("Usage: ci.py <start|wait> <owner/repo> <pr_number> <reviewed_head_sha>")
        mode, nwo, pr_number, head_sha = argv
        if mode == "start":
            payload = start(nwo, pr_number, head_sha)
        else:
            payload = wait(nwo, pr_number, head_sha, parse_json(sys.stdin.read(), "CI start input"))
    except QueryError as exc:
        payload = fatal(str(exc), run_ids=[])
    print(json.dumps(payload))


if __name__ == "__main__":
    main(sys.argv[1:])
