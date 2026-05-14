# Conductor Workflows

Sample workflow registry for [Conductor](https://github.com/microsoft/conductor).

## Workflows

| Name | Description |
|------|-------------|
| `document-review` | Review-only scoring of a markdown document on technical accuracy and readability, with threshold short-circuit |
| `sdd-plan` | Solution design + implementation plan with technical and readability review cycles |
| `sdd-plan-v3` | Quality-first design+plan workflow with research split, parallel reviewers, adversarial review, design/plan separation, and human gate |
| `sdd-implement` | Implement a plan epic-by-epic with epic-level and plan-level review |
| `sdd-implement-v3` | Quality-first implement workflow with cross-family epic review, adversarial plan review, and bounded fixer escalation |

## Usage

```bash
# Add this registry
conductor registry add sample /path/to/conductor-workflows --default

# List workflows
conductor registry list sample

# Run a workflow
conductor run sdd-plan --input goal="Design a caching layer"
```
