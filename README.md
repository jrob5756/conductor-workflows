# Conductor Workflows

Sample workflow registry for [Conductor](https://github.com/microsoft/conductor), plus a plugin marketplace that exposes workflows as skills.

## Workflows

| Name | Description |
|------|-------------|
| `document-create` | Create a new markdown document grounded in the codebase, with a structure inferred from the stated purpose, with technical and readability review cycles (loops back to the author until both thresholds are met) |
| `document-review` | Review-only scoring of a markdown document on technical accuracy and readability, with threshold short-circuit |
| `document-update` | Update an existing markdown document to incorporate a stated purpose, with technical and readability review cycles (loops back to the editor until both thresholds are met) |
| `fusion` | Multi-model deliberation modelled on OpenRouter's Fusion Router: a panel of models from different labs answers independently in parallel, an analyst compares (never merges) their answers into consensus, contradictions, coverage gaps, unique insights and blind spots, and a synthesiser writes the final answer. A cheap gate lets trivial questions skip the panel |
| `pr-review` | Multi-agent PR review with smart per-PR triage: a heuristic + cheap LLM decides which of 6 specialists (code, security, tests, errors, types, comments) need to run for this PR, then cross-lens deliberation and a non-blocking polish stage. Self-contained via the `gh` CLI; GitHub-only in v1 |
| `sdd-design` | Solution design document with technical and readability review cycles, a fixer agent applying targeted revisions between rounds, and a human gate when reviews don't converge (no implementation plan) |
| `sdd-plan` | Solution design + implementation plan with technical and readability review cycles, a fixer agent applying targeted revisions between rounds, and a human gate when reviews don't converge. Optional `design` input switches it to plan-only mode, consuming an existing design document (e.g. one produced by `sdd-design`) |
| `sdd-implement` | Implement a plan epic-by-epic with epic-level and plan-level review |
| `ship` | Take an existing GitHub issue to a merged pull request: cuts a worktree, plans behind a human question gate, implements unattended, opens a draft PR, runs a code review and reconciles the findings, then publishes and merges behind a human gate with full branch and worktree cleanup. A merge blocked by conflicts pauses to ask whether to resolve them, and `autopilot=true` bypasses every human gate |

## Usage

```bash
# Add this registry
conductor registry add sample /path/to/conductor-workflows --default

# List workflows
conductor registry list sample

# Run a workflow
conductor run sdd-plan --input goal="Design a caching layer"
```

## Plugin marketplace

This repo is also a plugin marketplace, so workflows can be invoked as skills
from Copilot CLI / VS Code and Claude Code.

```text
/plugin marketplace add jrob5756/conductor-workflows
/plugin install workflows@conductor-workflows
```

Then invoke a workflow directly:

```text
/workflows:fusion Compare ridge, lasso, and elastic-net regression. Where does each shine?
/workflows:ship 123
```

| Plugin | Skill | Runs |
|--------|-------|------|
| `workflows` | `fusion` | The `fusion` multi-model deliberation workflow |
| `workflows` | `ship` | The `ship` workflow, in the background — returns a dashboard URL to track it |

The skills resolve workflows through the Conductor registry rather than
bundling copies, so a skill and its workflow cannot drift apart. See
[`plugins/workflows/README.md`](plugins/workflows/README.md).
