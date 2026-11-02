---
name: sdd-plan
description: Start the Conductor sdd-plan workflow to write a reviewed solution design and implementation plan, or only the plan from an existing design. Thin wrapper over the conductor MCP sdd_plan tool.
argument-hint: "<purpose> [design <path>] [output <path>] [no questions] [rounds N]"
user-invocable: true
disable-model-invocation: true
---

# Conductor SDD Plan

Call the `conductor` MCP server's `sdd_plan` tool once, then stop.

- `purpose`: the arguments with the extras below removed. Pass it through unchanged, including any file paths or issue references in it. If none was given, ask for it.
- Map optional extras only when stated: "design <path>" → `design: <path>` (plan-only mode); "output <path>" → `output_path: <path>`; "no questions" → `allow_human_escalation: false`; "rounds N" → `max_review_rounds: N`.
- Leave `_wait_seconds` unset.

Report the run ID and dashboard URL. Don't write the plan yourself or poll the run.
If the tool is missing or the launch fails, report the exact error and stop.
