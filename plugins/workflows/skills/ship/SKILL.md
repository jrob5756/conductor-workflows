---
name: ship
description: Take an existing GitHub issue to a merged pull request by launching the ship Conductor workflow in the background and handing back its dashboard URL. The workflow cuts a worktree, plans behind a question gate, implements, opens a draft PR, reviews it and applies the findings, then publishes and merges behind a human gate, pausing again if the merge hits conflicts. Supports an autopilot mode that bypasses every gate. Use when the user invokes /workflows:ship, or asks to ship, implement, take on, or work an existing issue end to end — "ship #123", "implement issue 45", "take issue 7 to a PR". Do not use for creating issues, or for changes that have no issue.
argument-hint: issue number, URL, or owner/repo#123
---

# Ship

Launches the `ship` Conductor workflow with `--web-bg` and returns the
dashboard URL. The workflow runs in a detached background process: it survives
this session ending, and the user drives it from the dashboard.

This skill's whole job is **launch and hand off**. It does not implement the
issue, does not wait for the run, and does not report on work it did not
watch.

## Constraints

> **Never implement the issue yourself.** If `conductor` fails to launch — not
> installed, workflow not found, dirty repository, `gh` not authenticated —
> **report the exact error and stop.** Do not fall back to cutting a branch and
> writing the code by hand. The user asked for the orchestrated pipeline, with
> its planning gate, review stage and merge gate; a hand-rolled substitute
> skips all three and is not the same thing.

- **Never invent the dashboard URL.** It comes from the launch output or from
  `conductor status --json`. If neither yields one, say so.
- **Never report progress you have not observed.** After launch the run is the
  dashboard's to narrate. Do not guess which step it reached.
- **Never pass `-s`/`--silent`.** The `Dashboard:` line is only printed at
  normal verbosity, so silencing the launch throws away the one thing this
  skill exists to return.
- **The issue must already exist.** This workflow never creates one. If the
  user describes work with no issue, offer to file one first.

## Steps

### 1. Check the working directory

The workflow targets **the repository you launch it from** — it resolves the
repo root, default branch and `gh` identity from the current directory. Cutting
a worktree for the wrong repository wastes a full planning cycle, so confirm
before launching:

```bash
git rev-parse --show-toplevel && gh repo view --json nameWithOwner -q .nameWithOwner
```

If that fails, or names a repository the user did not mean, stop and ask.

### 2. Resolve the issue reference

Everything the user passed after the invocation is the issue. Accept a bare
number (`123`), a full URL, or `owner/repo#123` — the workflow resolves all
three. Pass it through unchanged; do not "helpfully" convert a number into a
URL.

If the user gave no issue, ask for one. Do not guess from recent context.

### 3. Resolve how to invoke the workflow

Use the first option that applies:

| Condition | Command form |
|---|---|
| cwd is inside a checkout containing `workflows/ship/workflow.yaml` | `conductor run workflows/ship/workflow.yaml` |
| a registry exposes `ship` (check `conductor registry list <name>`) | `conductor run ship@<registry>` |
| neither | add the registry first (below), then use `ship@conductor-workflows` |

```bash
conductor registry add conductor-workflows jrob5756/conductor-workflows
```

Never hardcode an absolute path, and never copy the workflow YAML into the
plugin — the registry is the supported way to resolve it.

Note the first row is unusual here: the workflow acts on the *current*
repository, so a local checkout of this registry is normally **not** where the
user wants to run. Prefer the registry form unless they are shipping an issue
in the registry repo itself.

### 4. Launch it

```bash
conductor run ship@conductor-workflows --input issue=123 --web-bg
```

`--web-bg` prints the dashboard URL and exits immediately. Do not use a long
timeout waiting for it — the command returns in seconds while the workflow runs
for as long as the work takes.

Pass `--input ask_questions=false` only when the user asks to skip the
planner's question gate. Leave it on by default: those questions are asked
before any code is written, and skipping them turns open questions into silent
assumptions.

Pass `--input autopilot=true` only when the user explicitly asks for an
unattended run — "don't ask me", "just ship it", "no gates". It removes **every**
human gate, including the merge approval, so the pull request lands on the
default branch with nobody having read it. Never turn it on to save the user a
step, and never infer it from impatience. If they ask for it, say plainly in
your reply that nothing will pause for them.

Autopilot still ends a run by itself in two cases, because neither is a human
approval: a code review reporting unresolved blocking findings or
`do_not_merge`, and merge conflicts it could not resolve. Both leave the pull
request open and fail the run rather than merging.

### 5. Return the dashboard URL

Parse the launch output:

```
Dashboard: http://127.0.0.1:34229
Child stderr log: /tmp/conductor/conductor-ship-...bg.stderr.log
```

If the `Dashboard:` line is missing but the launch reported no error, recover
it — this is the supported way, and it also works later in the session once the
launch output has scrolled away:

```bash
conductor status --json
```

That returns `{"running": [{"url": ..., "run_id": ..., "workflow": ..., "port": ..., "log_file": ...}]}`.
Match on `workflow` when more than one run is listed; never assume the first
entry is the one you just started.

Report, briefly:

- the dashboard URL, on its own line so it is clickable
- the issue being shipped
- that the run is in the background and survives this session
- **that it will stop and wait for them** — this is the part users miss, and
  the reverse under `autopilot=true`: say clearly that it will *not* stop, and
  will merge on its own

```
Shipping issue #123 — https://github.com/owner/repo/issues/123

Dashboard: http://127.0.0.1:34229

Running in the background; it will outlive this session. It pauses for you
three times: the planner's questions, plan approval, then the merge gate —
plus once more if the merge hits conflicts. Nothing is pushed before you
approve the plan, and nothing is merged before you approve the merge.
```

Then stop. Do not poll the dashboard, tail the log, or narrate the run.

### 6. If they come back later

`conductor status` lists running workflows read-only, with their URLs. Use it
to re-find a dashboard rather than launching a second run.

To stop a run, always name it — `conductor stop --run-id <id>`. A bare
`conductor stop` auto-stops when exactly one run is alive, which is precisely
when the user has the most to lose.

## What the workflow does

Worth summarising when the user has not run it before, so the pauses are not a
surprise:

| Step | What happens |
|---|---|
| `worktree_setup` | Assigns the issue, comments on it, cuts an isolated worktree |
| `planner` | Studies the repository and drafts a plan |
| `ask_questions` | Puts the planner's open questions to the user |
| `plan_approval` | **Human gate** — approve, revise, or stop |
| `coder` | Implements, then runs the repository's own build, lint and tests |
| `ship_draft` | Stages, commits, pushes, opens a **draft** PR |
| `review` | Code review — gathers the blocking and recommended changes, changes nothing |
| `review_fixer` | Applies those findings, re-runs the checks, pushes (skipped when the review found nothing) |
| `publish` | Marks the PR ready for review |
| `merge_gate` | **Human gate** — merge or leave it open |
| `merge` | Squash merges, deletes the branch, removes the worktree |
| `merge_conflicts` | **Human gate**, only when the branch conflicts with the base — resolve them and retry, or stop |
| `conflict_resolver` | Merges the base branch in, resolves the conflicts, re-runs the checks, pushes, and goes back to `merge` |

Declining the merge leaves the PR open and ends the run.

Under `autopilot=true` none of the gates appear: the questions and plan
approval are skipped, a stalled coder is retried `max_autopilot_rounds` times
(default 2) and then ships what exists, conflicts are resolved without asking,
and the merge happens unprompted.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `command not found: conductor` | CLI not installed | Report it; point at the Conductor install docs. Do not implement the issue by hand |
| `Workflow 'ship' not found` | registry missing | `conductor registry add conductor-workflows jrob5756/conductor-workflows` |
| `Extra inputs are not permitted` on `plugins` | Conductor predates plugin support | Report the version; the workflow needs a build with `plugins:` support |
| Launch succeeds, no `Dashboard:` line | `-s`/`--silent` was passed | Re-read it with `conductor status --json` |
| Workflow fails at `bootstrap` | `gh` unauthenticated, or not a git repository | `gh auth status`; confirm the working directory |
| Skill or subagent not found mid-run | required plugins not installed | The workflow declares them per step; install them from the marketplace it names |
| Dashboard URL refuses to connect | run already finished, or was stopped | `conductor status` to confirm whether it is still alive |
