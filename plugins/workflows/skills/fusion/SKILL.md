---
name: fusion
description: Run the fusion multi-model deliberation workflow — a panel of models from different labs answers a question independently in parallel, an analyst compares their answers into consensus, contradictions, coverage gaps, unique insights and blind spots, and a synthesiser writes the final answer. Use when the user invokes /workflows:fusion, or asks to deliberate on, get a second opinion on, stress-test, or convene a panel about a hard question — research questions, expert critique, compare-and-contrast prompts, architectural decisions, or anything where being wrong is expensive.
argument-hint: question to deliberate over
---

# Fusion

Runs the `fusion` Conductor workflow: a panel of models from different labs
answers the question independently and in parallel, an analyst **compares**
their answers (never merges them), and a synthesiser writes the final answer
from that comparison.

The value is the comparison. Independent agreement between models from
different labs is evidence; disagreement marks exactly where a single-model
answer would have been confidently incomplete.

## Constraints

> **Never answer the question yourself.** The entire point of this skill is
> multi-model deliberation. A single-model answer written by you is not a
> substitute, however good it looks.
>
> If `conductor` fails for any reason — not installed, provider error, workflow
> not found, timeout — **report the exact error and stop.** Do not simulate the
> panel, do not approximate it by reasoning harder, and do not quietly answer
> from your own knowledge. Ask the user how to proceed.

Two more rules:

- **Never invent the analysis.** `consensus`, `contradictions`, and
  `blind_spots` come from the workflow's JSON output or they are not reported.
- **Do not paraphrase the answer.** Present what the synthesiser wrote. You may
  add structure, but do not rewrite or "improve" its substance.

## Steps

### 1. Resolve how to invoke the workflow

Use the first option that applies:

| Condition | Command form |
|---|---|
| cwd is inside a checkout containing `workflows/fusion/workflow.yaml` | `conductor -s run workflows/fusion/workflow.yaml` |
| a registry exposes `fusion` (check `conductor registry list <name>`) | `conductor -s run fusion@<registry>` |
| neither | add the registry first (below), then use `fusion@conductor-workflows` |

```bash
conductor registry add conductor-workflows jrob5756/conductor-workflows
```

Never hardcode an absolute path, and never copy the workflow YAML into the
plugin — the registry is the supported way to resolve it.

### 2. Build the command

The question is everything the user passed after the skill invocation. Pass it
as a single `question` input, quoted.

```bash
conductor -s run fusion@conductor-workflows \
  --input question="Compare ridge, lasso and elastic-net regression. Where does each shine?" \
  --input mode=always
```

**Always pass `mode=always` unless the user says otherwise.** The workflow's
default `auto` mode runs a cheap gate that may decide the question isn't worth
a panel — which is the right default for programmatic use, but wrong here. A
user who explicitly invoked `/workflows:fusion` has already decided they want
deliberation, and silently returning a single-model answer would be surprising.

Map the user's intent onto the other inputs only when they ask:

A preset sets the panel **and** the analyst/synthesiser, so it controls both
cost and speed. Measured on one question: `fast` 79s, `balanced` 106s,
`quality` 273s.

| User says | Input |
|---|---|
| "quick", "cheap", "fast" | `--input preset=fast` |
| "best", "highest quality" | `--input preset=quality` (default) |
| "use every lab", "maximum diversity" | `--input preset=max` |
| names specific models | `--input panel='["gpt-5.6-sol", "grok-4.5"]'` |
| "dig into what they missed", "second round" | `--input max_rounds=2` |
| supplies a document, diff, or source to reason over | `--input context="..."` |

Valid model ids come from `conductor doctor --models -p copilot`. Never guess them.

### 3. Run it

Tell the user it's running **before** you start, with a realistic estimate:
roughly 80s at `preset=fast`, 105s at `balanced`, and 4-5 minutes at `quality`
or `max`. Double those for `max_rounds=2`.

Use a generous timeout (at least 900 seconds). `-s` prints only the final JSON,
which is what you parse.

### 4. Present the result

Parse the JSON. Fields:

| Field | Meaning |
|---|---|
| `answer` | The final answer — present this as the body of your reply |
| `deliberated` | `true` if the panel ran; `false` means it answered directly |
| `panel_models` | Which models actually responded |
| `rounds_run` | `0`, `1`, or `2` |
| `analysis` | JSON: `consensus`, `contradictions`, `coverage_gaps`, `unique_insights`, `blind_spots`, `confidence_notes` |

Lead with `answer`. Then, when the analysis contains them, add a short section
surfacing what a single model could not have told the user:

- **Where the panel disagreed** — from `contradictions`. This is the highest-value
  section; never drop it when it is non-empty.
- **Unresolved gaps** — from `blind_spots`, framed as limitations of the answer.

Skip empty sections rather than printing empty headings. Keep this digest
short; the user asked a question, not for a process report.

Close with a one-line footer naming the panel and rounds, so the cost is
visible and attributable:

```
Panel: claude-haiku-4.5, gpt-5.6-luna, gemini-3.5-flash · 1 round
```

If `deliberated` is `false`, say so plainly — the answer came from a single
model, not a panel.

## Cost

Deliberation runs N panel calls plus an analyst and a synthesiser: roughly 4-5x
a single completion with the default 3-model panel, and `max_rounds=2` roughly
doubles the panel portion again. If the user seems cost-sensitive, or the
question is simple enough that a panel is overkill, say so and offer
`preset=fast` before running — don't silently spend.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `command not found: conductor` | CLI not installed | Report it; point at the Conductor install docs. Do not answer the question yourself |
| `Workflow 'fusion' not found` | registry missing | `conductor registry add conductor-workflows jrob5756/conductor-workflows` |
| `Model "X" is not available` | stale/invalid model id in `panel` | Re-check with `conductor doctor --models -p copilot` |
| Run exceeds the timeout | large panel or `max_rounds=2` | Re-run with `preset=fast`, or raise the timeout |
| `deliberated: false` unexpectedly | `mode=always` was not passed | Re-run with `--input mode=always` |
