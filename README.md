# Conductor Workflows

Sample workflow registry for [Conductor](https://github.com/microsoft/conductor).

## Workflows

| Name | Description |
|------|-------------|
| `qa-bot` | Simple single-agent Q&A |
| `summarizer` | Two-stage summarizer (draft → refine), uses `!file` for prompts |
| `code-review` | Multi-agent code review with reviewer and critic |

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
