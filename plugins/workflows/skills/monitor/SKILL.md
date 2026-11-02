---
name: monitor
description: Watch a running Conductor workflow on a schedule (every 10 minutes by default). On a failure, investigate, fix the cause, and resume from its checkpoint, unless the failure needs a human, in which case report it and stop. Stacks with the other workflows skills.
argument-hint: "[run-id] [every <interval>]"
user-invocable: true
disable-model-invocation: true
---

# Conductor Monitor

Babysits one Conductor run so the user doesn't have to. Use it after a launcher
skill (`/workflows:ship`, `/workflows:pr-review`, `/workflows:sdd-design`,
`/workflows:sdd-plan`, `/workflows:sdd-implement`) or on any run ID.

The monitor keeps the **run** moving. It never does the **workflow's work**:
it does not write the design, implement the code, or review the PR when a step
fails. Fixes are limited to the environment and run state so the workflow can
carry on by itself.

## 1. Resolve the run and interval

- **Run ID:** the argument if given. Otherwise, the run launched earlier in
  this conversation. Otherwise, call `conductor_list_runs` and use the single
  live run. If that leaves zero or several candidates, ask. Never guess.
- **Interval:** `every <interval>` from the arguments (`5m`, `15 minutes`,
  `1h`). Default `10m`.
- **Launch directory:** `system.cwd` from the run's newest checkpoint if one
  exists (see step 5 of the tick). Otherwise the current cwd, since the MCP
  server launches runs from the host's working directory.

## 2. Tick once now, then schedule

Run the tick procedure below right away, substituting `<RUN_ID>`,
`<WORKFLOW>`, `<CWD>` and `<INTERVAL>`. If the run is still live afterwards,
schedule the same text, verbatim with the values filled in, as a recurring
prompt:

- **Copilot CLI:** `manage_schedule` with `action: "create"`,
  `interval: "<INTERVAL>"`, `prompt: <tick text>`.
- **Claude Code:** the recurring-prompt tool (`CronCreate`), or tell the user to
  run `/loop <INTERVAL> <tick text>`.
- **No scheduler:** say so, print the tick text, and stop. Don't busy-wait.

The skill isn't model-invocable, so the scheduled text has to work on its own.
That is why the whole procedure lives in it.

Reply with the run ID, dashboard URL, interval, and how to stop the monitor
(`manage_schedule` `stop`, or ask to stop monitoring `<RUN_ID>`).

## Tick procedure

```text
Conductor monitor tick for run <RUN_ID> (<WORKFLOW>), launched from <CWD>, every <INTERVAL>.
Do not do the workflow's own work. Only keep the run moving.

State file: S="$(python3 -c 'import tempfile;print(tempfile.gettempdir())')/conductor-monitor/<RUN_ID>.json"
holding {"resumes": 0, "last_failure": null, "notified_gate": null, "last_step": null, "step_since": null}.
Create it if it is missing. Update it on every tick.

1. Call the conductor MCP tool conductor_run_status(run_id="<RUN_ID>").
2. Act on status:
   - running: if current_step changed, record it with the time. If it has been unchanged for
     more than 3 intervals, call conductor_run_events(run_id, limit=5). If there are no new events
     in that window, report a possible stall once and do not kill the run. Otherwise reply in one
     line: "<RUN_ID>: running at <step>".
   - at-gate or paused: a human is needed. If this gate is not notified_gate yet, report the gate
     prompt, its options and approval_url, then record it. Never answer a gate yourself. Keep the
     schedule.
   - completed: summarise the output in a few lines, delete the state file, stop this schedule.
   - failed or unknown: go to 3.
3. Investigate. Read error_type and error_message. Call
   conductor_run_events(run_id, event_types=["agent_failed","script_failed","subworkflow_failed",
   "agent_timeout","agent_validation_failed","workflow_failed"]). Call conductor_node_detail on the
   failing step and conductor_run_logs, then read the logs. Find the root cause, not just the symptom.
4. Decide. A human is needed if ANY of these hold:
   - the user or the dashboard stopped or cancelled the run
   - auth, credentials, secrets, permissions, or access are the cause
   - an iteration, timeout, or cost limit was hit
   - the root cause is a bug in the workflow YAML or in Conductor itself
   - the fix needs a product or design decision, a destructive git operation (force push, reset,
     deleting branches or worktrees with work in them), or code changes outside the run's own work
   - the failure signature (step + error_type + gist of the message) equals last_failure
   - resumes >= 3
   - the cause is unclear
   If a human is needed: report the diagnosis, the evidence, the proposed fix, and the exact resume
   command. Keep the state file and stop this schedule.
   Otherwise the cause is transient (provider or network error, rate limit, session timeout, killed
   process) or a local environment problem with a safe, reversible fix (stale lock file, missing
   installable tool or plugin, port in use, broken npx cache, leftover state the run itself
   created). Fix it, then go to 5.
5. Resume. Find the checkpoint: the newest *.json under
   "$(python3 -c 'import tempfile;print(tempfile.gettempdir())')/conductor/checkpoints" whose
   "run_id" is "<RUN_ID>". Prefer trigger "failure". After a crash, use the newest "periodic" one.
   If there is none, a human is needed (see 4). cd to the checkpoint's system.cwd and run:
     conductor resume --from <checkpoint> --web-bg --guidance "<one line: what failed and what was fixed>"
   The resumed run keeps run_id <RUN_ID>. Increment resumes, set last_failure, and report the
   cause, the fix, and the new dashboard URL. Keep the schedule.
```

## Rules

- **Never answer a human gate or question** for the user, even when the answer
  looks obvious. Gates are the user's decisions.
- **Never resume a run a person stopped.** A "stopped by user" failure is a
  decision, not an error.
- **Never use `--skip-gates`** on resume, and never pass `autopilot` or turn off
  `allow_human_escalation` to get past a failure.
- **Never edit the workflow YAML** or the registry cache to make a run pass.
  Report the bug instead.
- **Never invent status.** Every claim comes from `conductor_run_status`, the
  events, the logs, or the checkpoint.
- Keep quiet ticks to one line. Speak up only on a change: a gate, a failure, a
  fix and resume, or completion.
