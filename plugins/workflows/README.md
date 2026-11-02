# workflows (Copilot / Claude Code plugin)

Invokes Conductor workflows from the [`conductor-workflows`](https://github.com/jrob5756/conductor-workflows)
registry as skills.

## What this plugin contains

| Skill | Runs |
|-------|------|
| `ship` | The `ship` workflow: takes an existing GitHub issue to a merged pull request |
| `pr-review` | The `pr-review` workflow on an existing pull request |
| `sdd-design` | The `sdd-design` workflow: a reviewed solution design document |
| `sdd-plan` | The `sdd-plan` workflow: design plus implementation plan, or the plan only from an existing design |
| `sdd-implement` | The `sdd-implement` workflow: implements a plan epic by epic |
| `monitor` | Watches a run on a schedule, fixes and resumes it after a failure, and stops to ask you when a failure needs a human |

Plus `.mcp.json`, which registers the Conductor MCP server
(`conductor mcp serve`). The skills call its tools to launch and inspect runs.

Every skill is **manual only** (`disable-model-invocation: true`). The model
never starts a workflow on its own. You invoke the skill.

The skills don't bundle any workflow YAML. The MCP server resolves workflows
from your configured Conductor registries, so a skill and its workflow can't
drift apart.

## Install

```text
/plugin marketplace add jrob5756/conductor-workflows
/plugin install workflows@conductor-workflows
```

## Use

```text
/workflows:ship 123
/workflows:pr-review owner/repo#45 no merge
/workflows:sdd-design Add OAuth2 login output docs/auth.design.md
/workflows:sdd-plan Add OAuth2 login design docs/auth.design.md
/workflows:sdd-implement docs/auth.plan.md
/workflows:monitor
```

The launcher skills start the workflow in the background, report its run ID
and dashboard URL, and stop. Answer gates and questions in the dashboard.

### Monitoring a run

Run `/workflows:monitor` right after a launcher skill and it picks up that run.
You can also name the run and interval: `/workflows:monitor 01ec590f every 15m`.
The default interval is 10 minutes.

On each tick the monitor checks the run's status and acts on it:

- **Running:** reports one line, or flags a possible stall. It never kills a run.
- **At a gate:** tells you once, with the approval URL. It never answers a gate
  for you.
- **Failed:** investigates the events, logs, and checkpoint. If the cause is
  transient, or a local environment problem with a safe fix, it fixes it and
  runs `conductor resume --from <checkpoint> --web-bg`. A resumed run keeps its
  run ID.
- **Needs you:** the monitor reports the diagnosis and stops. That happens when
  the run was stopped by a person, the cause is auth or permissions, a limit
  was hit, the workflow itself is buggy, the same failure repeats, three resumes
  are used up, or the cause is unclear.
- **Completed:** summarises the output and stops.

The monitor never does the workflow's own work, never uses `--skip-gates`, and
never edits workflow YAML.

Scheduling uses the host's recurring prompts: `manage_schedule` in Copilot CLI,
and `CronCreate` or `/loop` in Claude Code.

## Prerequisites

- The `conductor` CLI on `PATH` — see the [Conductor install docs](https://github.com/microsoft/conductor#installation).
- The workflow registry, added once (then restart the session, because the MCP
  server reads its catalogue only at startup):

  ```bash
  conductor registry add conductor-workflows jrob5756/conductor-workflows
  ```

- Each workflow's own requirements: `git`, `gh` (authenticated), `python3`, and
  Node.js 18+ for the workflows' MCP servers, plus any plugins a workflow declares.

## Local development

Point the CLI at this directory directly:

```bash
claude --plugin-dir plugins/workflows
```
