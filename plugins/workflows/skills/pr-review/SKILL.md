---
name: pr-review
description: Start the Conductor pr-review workflow for an existing pull request (number, URL, or owner/repo#N). Thin wrapper over the conductor MCP pr_review tool.
argument-hint: "<pr> [no merge] [ignore <check>...]"
user-invocable: true
disable-model-invocation: true
---

# Conductor PR Review

Call the `conductor` MCP server's `pr_review` tool once, then stop.

- `pr`: the PR from the arguments (number, URL, or `owner/repo#N`). If none was given, ask for it.
- Map optional extras only when stated: "no merge" → `merge: false`; "ignore <check>" → `ignored_checks: [...]`.
- Leave `_wait_seconds` unset.

Report the run ID and dashboard URL. Don't review the PR yourself or poll the run.
If the tool is missing or the launch fails, report the exact error and stop.
