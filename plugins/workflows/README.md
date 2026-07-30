# workflows (Copilot / Claude Code plugin)

Invokes Conductor workflows from the [`conductor-workflows`](https://github.com/jrob5756/conductor-workflows)
registry as skills.

## What this plugin contains

- `skills/fusion/SKILL.md` — runs the `fusion` multi-model deliberation workflow.

This plugin ships **only markdown** — no executables, hooks, MCP servers, or
custom agents — so trust verification is straightforward: read the markdown.

The skill does not bundle a copy of any workflow YAML. It resolves workflows
through the Conductor registry, so the workflow and the skill cannot drift apart.

## Install

```text
/plugin marketplace add jrob5756/conductor-workflows
/plugin install workflows@conductor-workflows
```

## Use

```text
/workflows:fusion Compare ridge, lasso, and elastic-net regression. Where does each shine?
```

The skill also triggers on natural language — "get a second opinion on…",
"convene a panel about…", "stress-test this decision…".

## Prerequisites

- The `conductor` CLI on `PATH` — see the [Conductor install docs](https://github.com/microsoft/conductor#installation).
- The workflow registry, added once:

  ```bash
  conductor registry add conductor-workflows jrob5756/conductor-workflows
  ```

- Node.js 18+ (the fusion workflow's panel uses the `open-websearch` MCP server).

## Local development

Point the CLI at this directory directly:

```bash
claude --plugin-dir plugins/workflows
```
