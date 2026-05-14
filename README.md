# Conductor Workflows

Sample workflow registry for [Conductor](https://github.com/microsoft/conductor).

## Workflows

| Name | Description |
|------|-------------|
| `document-review` | Review-only scoring of a markdown document on technical accuracy and readability, with threshold short-circuit |
| `pr-review` | Multi-agent PR review: 5 deep-domain specialists (code, tests, errors, types, comments) → cross-lens deliberation → non-blocking polish stage. Self-contained via the `gh` CLI; GitHub-only in v1 |
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
