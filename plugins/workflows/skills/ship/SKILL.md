---
name: ship
description: Start the Conductor ship workflow to take an existing GitHub issue (number, URL, or owner/repo#N) to a merged pull request. Thin wrapper over the conductor MCP ship tool.
argument-hint: "<issue> [autopilot] [no questions] [rounds N]"
user-invocable: true
disable-model-invocation: true
---

# Conductor Ship

Call the `conductor` MCP server's `ship` tool once, then stop.

- `issue`: the issue from the arguments (number, URL, or `owner/repo#N`). If none was given, ask for it.
- Map optional extras only when stated: "autopilot" → `autopilot: true`; "no questions" → `ask_questions: false`; "rounds N" → `max_autopilot_rounds: N`.
- Leave `_wait_seconds` unset.

Report the run ID and dashboard URL. Don't implement the issue yourself or poll the run.
If the tool is missing or the launch fails, report the exact error and stop.
