# fusion

Multi-model deliberation, modelled on [OpenRouter's Fusion Router](https://openrouter.ai/docs/guides/routing/routers/fusion-router).

Ask a hard question. A panel of models from different labs answers it independently and in parallel. An analyst then **compares** those answers — it deliberately does not merge them — into structured analysis: consensus, contradictions, coverage gaps, unique insights, and blind spots. A synthesiser writes the final answer from that analysis.

The premise is that independent agreement is evidence. Because panelists cannot see each other, when three models from three different labs converge on a claim, that convergence carries information a single model's confidence does not. Equally, where they diverge is exactly where a single-model answer would have been confidently incomplete.

Use it when a single model isn't enough: research questions, expert critique, "compare and contrast" prompts, architectural decisions — anything where the cost of being wrong outweighs the cost of a few extra completions.

## File layout

```
workflows/fusion/
  workflow.yaml   the entire pipeline, self-contained
  README.md
```

One file. It runs directly, and because any Conductor workflow can be called with `type: workflow`, the same file doubles as an embeddable deliberation step (see [As a sub-workflow](#as-a-sub-workflow)) with no wrapper needed.

## Pipeline

```
resolve_config (set)   preset → panel[{model, role}], analyst, synthesiser, mode
  ↓
gate (cheap LLM)       is this question worth deliberating over?
  ├─ no  → direct_answer → $end          fast path, single model
  └─ yes ↓
panel (for_each)       N models answer independently, in parallel, with web
                       search. key_by model → per-model attribution.
  ↓
panel_check (set)      if every panelist failed, fall back to direct_answer
  ↓
analyst                consensus / contradictions / coverage_gaps /
                       unique_insights / blind_spots   (structured output)
  ↓
round_check (set)      max_rounds > 1 and blind spots found?
  ├─ yes → gap_panel → analyst_merge ↓     targeted second pass
  └─ no  ───────────────────────────↓
synthesizer            final answer grounded in the analysis
  ↓ $end
```

### The analyst compares; it never merges

This is the load-bearing idea, and the easiest thing to get wrong. If the analyst merges the panel's answers, it produces a blended consensus blob and the disagreement signal — the most valuable output — disappears. Merging is the synthesiser's job, and only after the comparison exists. The analyst's system prompt and its strict output schema both enforce the separation.

### The blind-spot round

Fusion is bounded to a single deliberation level by design. Because this is a workflow engine rather than a latency-bound router, `max_rounds: 2` adds something Fusion structurally cannot do: when the analyst identifies things *no* panelist addressed, the panel runs again aimed squarely at those gaps, and the analyst folds the results back in.

It is off by default to stay faithful to Fusion and to keep costs predictable. It is implemented as a separate `gap_panel` group rather than a loop-back into `panel`, because a loop-back would need a round counter and `set` steps render against pre-step context — a distinct group is bounded by construction.

## Prerequisites

- Node.js 18+ (for the `open-websearch` MCP server used by the panel)

## Inputs

| Input | Default | Description |
|---|---|---|
| `question` | *(required)* | The question or task to deliberate over |
| `context` | `""` | Background material handed verbatim to every panelist |
| `preset` | `quality` | Preset for panel + analyst + synthesiser — see below |
| `panel` | `[]` | Explicit panel, e.g. `["gpt-5.6-sol", "grok-4.5"]`. 1–8 models |
| `mode` | `auto` | `auto` = gate decides; `always` = always deliberate; `never` = single-model answer |
| `max_rounds` | `1` | `2` enables the targeted blind-spot round |
| `gate_model` | `gpt-5.6-luna` | Decides whether to deliberate; one call, negligible share of total cost |
| `analyst_model` | *(preset)* | Compares the panel's answers. Must support `high` reasoning effort |
| `synthesizer_model` | *(preset)* | Writes the final answer; also serves the fast path |

`mode: always` is the equivalent of Fusion's `tool_choice: "required"`.

### Presets

A preset governs **all three stages** — panel, analyst, and synthesiser.

| Preset | Panel | Analyst + synthesiser | Measured |
|---|---|---|---|
| `quality` *(default)* | `claude-opus-5`, `gpt-5.6-sol`, `gemini-3.1-pro-preview` | `claude-opus-5` | 273s |
| `balanced` | `claude-sonnet-5`, `gpt-5.6-terra`, `gemini-3.1-pro-preview` | `claude-sonnet-5` | 106s |
| `fast` | `claude-haiku-4.5`, `gpt-5.6-luna`, `gemini-3.5-flash` | `claude-sonnet-5` | 79s |
| `max` | the `quality` panel plus `grok-4.5` and `claude-sonnet-5` | `claude-opus-5` | ~290s |

`quality` mirrors Fusion's own Quality preset (Opus + GPT + Gemini Pro); `fast`
mirrors `general-fast`. Panel value comes from *diversity*, so prefer spreading
across labs over stacking several models from one.

The `panel` input overrides the panel only. `analyst_model` and
`synthesizer_model` override those independently; leave them empty to take the
preset's.

**Why the preset also sets the analyst.** The panel is only 13–53% of wall
time — the analyst and synthesiser dominate. When presets changed the panel
alone, `fast` (176s) was no quicker than `balanced` (169s), because both still
ran `claude-opus-5` for the last two stages. Coupling all three is what makes
the names honest.

**Why `claude-sonnet-5` is the floor for the analyst.** It is the only stage
that produces `blind_spots`, and unlike a weak panelist it has no backstop — the
other panelists and the analyst cover for a poor panel answer, but nothing
covers for a poor analyst. It must also support `high` reasoning effort, which
rules out `claude-haiku-4.5`, `grok-4.5` and `gemini-3.6-flash` outright: they
advertise no reasoning support and the Copilot provider rejects the pair at run
time.

List valid model ids with `conductor doctor --models -p copilot`.

## Outputs

| Output | Description |
|---|---|
| `answer` | The final answer |
| `deliberated` | `true` if the panel ran, `false` if answered directly |
| `panel_models` | Comma-separated models that actually responded |
| `rounds_run` | `0` (direct), `1`, or `2` |
| `analysis` | The full structured analysis as JSON |

The shape is identical on every path, so callers embedding this get a stable contract.

## Examples

### Basic

```bash
conductor run workflows/fusion/workflow.yaml \
  --input question="What are the strongest arguments for and against carbon taxes?"
```

### Force deliberation, cheap panel

```bash
conductor run workflows/fusion/workflow.yaml \
  --input question="Compare ridge, lasso and elastic-net regression." \
  --input mode=always \
  --input preset=fast
```

### Bring your own panel, chase blind spots

```bash
conductor run workflows/fusion/workflow.yaml \
  --input question="Is event sourcing worth it for a 5-person team?" \
  --input panel='["claude-opus-5", "gpt-5.6-sol", "grok-4.5"]' \
  --input max_rounds=2
```

### Deliberate over material you supply

```bash
conductor run workflows/fusion/workflow.yaml \
  --input question="Which of these two migration plans is riskier, and why?" \
  --input context="$(cat plans.md)" \
  --input mode=always
```

## As a sub-workflow

`deliberate` is self-contained and can be dropped into any workflow as a deliberation step — the Conductor-native equivalent of Fusion's `openrouter:fusion` server tool:

```yaml
- name: deliberate
  type: workflow
  workflow: ../fusion/workflow.yaml
  input_mapping:
    question: "{{ planner.output.open_question }}"
    context: "{{ research.output.findings }}"
    preset: "balanced"
    mode: "always"
  routes:
    - to: next_step
```

Then read `{{ deliberate.output.answer }}` and `{{ deliberate.output.analysis }}`.

## Cost

Deliberation runs N panel calls plus an analyst call plus a synthesiser call. With the default 3-model panel, expect roughly 4–5× a single completion; `max_rounds: 2` roughly doubles the panel portion again. Cost scales linearly with panel size.

Controls, cheapest first:

- `mode: auto` (default) — trivial questions never convene the panel
- `preset: fast` — the whole pipeline for a fraction of `quality`, and ~3.5x
  faster (79s vs 273s measured on the same question)
- a smaller `panel` — two models still produce useful contradiction signal
- `analyst_model` / `synthesizer_model` — these dominate wall time, so they are
  the strongest latency lever as well as a cost one

Latency note: the panel runs in parallel, so it finishes only when its *slowest*
member does. On one `quality` run two panelists finished in ~22s while a third
took 143s doing web research — 44% of total runtime spent waiting on one model.
Adding a panelist is therefore close to free in wall time; it costs money, not
minutes.

## Implementation notes

Three Conductor behaviours this workflow depends on, each of which fails quietly if you get it wrong:

- **`key_by: model`, not `key_by: panelist.model`.** The path is relative to the loop item, not prefixed with the loop variable. Prefixing silently falls back to integer keys, and the analyst then sees "Panelist 0/1/2" instead of model names — losing attribution with no error raised.
- **No `reasoning.effort` on panel agents.** Panel models are dynamic, and some (`grok-4.5`, `gemini-3.6-flash`) advertise no reasoning support; the Copilot provider raises `ValidationError` on unsupported model/effort pairs, so setting it would break any panel containing them.
- **A for-each `source` needs a 3-part dotted path.** Hence `resolve_config` uses `values:` (producing `resolve_config.output.panel`) rather than a bare `value:`.

One upstream constraint is worth knowing: the analyst cannot be pinned to temperature 0 the way Fusion pins it. Conductor has no per-agent `temperature` (it is runtime-wide only), and the Copilot provider discards `runtime.temperature` outright — it logs `Copilot SDK does not support 'temperature' as a session parameter` and ignores the value. Analyst determinism therefore rests entirely on its system prompt and its strict output schema. Setting a temperature here would be a no-op.

## Known limitations

- The panel is single-provider (`copilot`). Genuine cross-provider panels (Anthropic direct, etc.) would need per-item `provider` overrides.
- Panelists get the question and `context` only; they have no repository access. For code-grounded deliberation, pass the relevant source in `context`.
- `max_rounds` is capped at 2 by construction.
