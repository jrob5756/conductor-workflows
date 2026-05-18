# Conductor Workflows

Sample workflow registry for [Conductor](https://github.com/microsoft/conductor).

## Workflows

| Name | Description |
|------|-------------|
| `document-create` | Create a new markdown document grounded in the codebase, with a structure inferred from the stated purpose, with technical and readability review cycles (loops back to the author until both thresholds are met) |
| `document-review` | Review-only scoring of a markdown document on technical accuracy and readability, with threshold short-circuit |
| `document-update` | Update an existing markdown document to incorporate a stated purpose, with technical and readability review cycles (loops back to the editor until both thresholds are met) |
| `pr-review` | Multi-agent PR review with smart per-PR triage: a heuristic + cheap LLM decides which of 6 specialists (code, security, tests, errors, types, comments) need to run for this PR, then cross-lens deliberation and a non-blocking polish stage. Self-contained via the `gh` CLI; GitHub-only in v1 |
| `sdd-design` | Solution design document with technical and readability review cycles (no implementation plan) |
| `sdd-plan` | Solution design + implementation plan with technical and readability review cycles. Optional `design` input switches it to plan-only mode, consuming an existing design document (e.g. one produced by `sdd-design`) |
| `sdd-implement` | Implement a plan epic-by-epic with epic-level and plan-level review |

## Usage

```bash
# Add this registry
conductor registry add sample /path/to/conductor-workflows --default

# List workflows
conductor registry list sample

# Run a workflow
conductor run sdd-plan --input goal="Design a caching layer"
```
