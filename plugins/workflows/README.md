# workflows (Copilot / Claude Code plugin)

Invokes Conductor workflows from the [`conductor-workflows`](https://github.com/jrob5756/conductor-workflows)
registry as skills.

## What this plugin contains

- `skills/fusion/SKILL.md` — runs the `fusion` multi-model deliberation workflow.
- `skills/ship/SKILL.md` — runs the `ship` workflow in the background and hands
  back its dashboard URL.

This plugin ships **only markdown** — no executables, hooks, MCP servers, or
custom agents — so trust verification is straightforward: read the markdown.

The skills do not bundle a copy of any workflow YAML. They resolve workflows
through the Conductor registry, so a workflow and its skill cannot drift apart.

## Install

```text
/plugin marketplace add jrob5756/conductor-workflows
/plugin install workflows@conductor-workflows
```

## Use

```text
/workflows:fusion Compare ridge, lasso, and elastic-net regression. Where does each shine?
/workflows:ship 123
```

Both skills also trigger on natural language — "get a second opinion on…",
"convene a panel about…" for `fusion`; "ship issue 45", "take #7 to a PR" for
`ship`.

`ship` launches with `--web-bg`, so the run outlives the session that started
it. The skill returns the dashboard URL and stops; the workflow pauses there
for the planner's questions, plan approval, and the merge gate. Use
`conductor status` to re-find a dashboard later.

## Prerequisites

- The `conductor` CLI on `PATH` — see the [Conductor install docs](https://github.com/microsoft/conductor#installation).
- The workflow registry, added once:

  ```bash
  conductor registry add conductor-workflows jrob5756/conductor-workflows
  ```

- Node.js 18+ (the fusion workflow's panel uses the `open-websearch` MCP server).
- For `ship`: `git`, `gh` (authenticated), and `python3` on `PATH`, plus the
  plugins the workflow declares per step.

## Local development

Point the CLI at this directory directly:

```bash
claude --plugin-dir plugins/workflows
```
