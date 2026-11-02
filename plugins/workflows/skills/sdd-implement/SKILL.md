---
name: sdd-implement
description: Start the Conductor sdd-implement workflow to implement an implementation plan epic by epic in an isolated git worktree, with epic- and plan-level review. Thin wrapper over the conductor MCP sdd_implement tool.
argument-hint: "<plan path> [epic <id>] [branch <name>] [no questions] [rounds N]"
user-invocable: true
disable-model-invocation: true
---

# Conductor SDD Implement

Call the `conductor` MCP server's `sdd_implement` tool once, then stop.

- `plan`: the plan path (or plan content) from the arguments, unchanged. If none was given, ask for it.
- Map optional extras only when stated: "epic <id>" → `epic: <id>`; "branch <name>" → `branch: <name>`; "no questions" → `allow_human_escalation: false`; "rounds N" → `max_epic_review_rounds: N`.
- Leave `_wait_seconds` unset.

The workflow creates or reuses its own worktree, so don't create one first.

Report the run ID and dashboard URL. Don't implement the plan yourself or poll the run.
If the tool is missing or the launch fails, report the exact error and stop.
