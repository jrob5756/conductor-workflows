---
name: sdd-design
description: Start the Conductor sdd-design workflow to write a reviewed solution design document (no implementation plan). Thin wrapper over the conductor MCP sdd_design tool.
argument-hint: "<purpose> [output <path>] [no questions] [rounds N]"
user-invocable: true
disable-model-invocation: true
---

# Conductor SDD Design

Call the `conductor` MCP server's `sdd_design` tool once, then stop.

- `purpose`: the arguments with the extras below removed. Pass it through unchanged, including any file paths or issue references in it. If none was given, ask for it.
- Map optional extras only when stated: "output <path>" → `output_path: <path>`; "no questions" → `allow_human_escalation: false`; "rounds N" → `max_review_rounds: N`.
- Leave `_wait_seconds` unset.

Report the run ID and dashboard URL. Don't write the design yourself or poll the run.
If the tool is missing or the launch fails, report the exact error and stop.
