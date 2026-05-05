# Conductor Workflows

Sample workflow registry for [Conductor](https://github.com/microsoft/conductor).

## Workflows

| Name | Description |
|------|-------------|
| `qa-bot` | Simple single-agent Q&A |
| `summarizer` | Two-stage summarizer (draft → refine), uses `!file` for prompts |
| `code-review` | Multi-agent code review with reviewer and critic |
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
conductor run qa-bot --input question="What is Python?"
conductor run summarizer --input text="Long text to summarize..."
conductor run code-review --input code="def foo(): pass"
```
