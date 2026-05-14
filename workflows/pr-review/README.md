# pr-review

Multi-agent PR review combining specialist depth with structured deliberation.

This workflow runs **6 deep-domain specialists** (general code, security,
tests, error handling, type design, comments) in parallel against a GitHub
PR diff, consolidates their findings, then deliberates the consolidated set
through **2 cross-family adversarial lens reviewers** (Skeptic vs
Completeness) until they reach consensus or exhaust the round budget.
Finally, a **non-blocking polish stage** (code-simplifier + dead-code-finder)
surfaces advisory cleanup opportunities.

The result is one rendered review comment plus a JSON list of inline comments
ready to post on the PR — optionally posted automatically via the `gh` CLI.

The workflow is composed of **four sub-workflows** (one per phase) plus a slim
parent that orchestrates them. Each sub-workflow can also be invoked directly
for debugging.

---

## File layout

```
workflows/pr-review/
├── workflow.yaml              # Slim parent — pr_fetcher → dispatcher → 4 sub-workflows → poster
├── README.md                  # This file
├── scripts/
│   ├── fetch-pr.sh            # gh CLI: fetch PR metadata + diff + changed files
│   ├── post-review.sh         # gh CLI: post comment + inline comments back to PR
│   ├── persist-json.sh        # Writes a JSON string to a file
│   └── persist-multi-json.sh  # Writes multiple JSON strings to multiple files
└── subworkflows/
    ├── specialists.yaml       # 5 specialists in parallel + gate + consolidator + persist
    ├── deliberate.yaml        # 2 lens reviewers in parallel + deliberation rounds + arbitrator + persist
    ├── polish.yaml            # 2 polish agents in parallel + persist
    └── render.yaml            # report_writer + inline_comments_writer + output_writer
```

## Pipeline

```
pr_fetcher (gh CLI script — no MCP, no LLM)
  ↓
dispatcher (validate, nonce-wrap diff, load context)
  ↓
specialists (sub-workflow)
  ├─ specialist_code         (project-guideline compliance, bugs, quality)
  ├─ specialist_security     (CVSS-style vulnerability review — injection, auth, crypto, secrets, …)
  ├─ specialist_tests        (behavioural coverage, edge cases)
  ├─ specialist_errors       (silent failures, broad catches, hidden errors)
  ├─ specialist_types        (encapsulation, invariants × 4 dimensions)
  ├─ specialist_comments     (comment accuracy, doc rot)
  ├─ specialist_gate         (phantom-reviewer detection)
  ├─ consolidator            (normalise native severities, dedupe, rank)
  └─ persist_specialist_outputs  (writes specialist-outputs.json + gate-output.json + …)
  ↓
deliberate (sub-workflow)
  ├─ lens_skeptic                 (claude-opus-4.7 default — "are these findings real?")
  ├─ lens_completeness            (gpt-5.5 default — "what did we miss?", cross-family)
  ├─ deliberation rounds (sequential loop, 1-10):
  │   deliberation_lens_1 → deliberation_lens_2 → arbitrator
  │   ↳ arbitrator: another round, or move on
  └─ persist_deliberation_outputs  (writes lens-outputs.json + arbitrator-output.json + …)
  ↓
polish (sub-workflow — non-blocking, advisory only)
  ├─ polish_simplifier   (clarity / refactor opportunities)
  ├─ polish_dead_code    (unused / unreachable code, with verification)
  └─ persist_polish_outputs  (writes polish-outputs.json)
  ↓
render (sub-workflow)
  ├─ report_writer          (composes review markdown — full or delta)
  ├─ inline_comments_writer (builds inline_comments JSON array)
  └─ output_writer          (writes pr-N-review.md + versioned archives + inline JSON)
  ↓
[optional] poster (gh CLI script — gated on auto_post=true)
  ↓
$end
```

## Why sub-workflows?

The original v1.0.0 workflow was a single 3,470-line YAML. v1.1.0 splits it
into a slim parent orchestrator plus four phase-specific sub-workflows for
several reasons:

1. **Each file has one responsibility** — easier to reason about, edit, and review.
2. **Sub-workflows are independently invokable for debugging** — you can run
   `conductor run workflows/pr-review/subworkflows/specialists.yaml` against
   pre-prepared inputs to iterate on the specialist agents without re-running
   the entire pipeline.
3. **Iteration budgets are scoped** — each sub-workflow has its own
   `max_iterations` cap, so a runaway loop in one phase can't exhaust the
   parent's budget.
4. **Sub-workflow outputs are explicit** — the `output:` section of each
   sub-workflow declares exactly what flows back to the parent, which is
   easier to track than a flat 20-agent shared scope.

Data flow: each sub-workflow persists its raw outputs to JSON files in
`pr_data_dir` so the render sub-workflow can reference them, and pipes
summary metadata (counts, status flags, key findings) back through normal
sub-workflow output for the parent and downstream sub-workflows to consume.

## Prerequisites

- **`gh` (GitHub CLI)** on PATH, authenticated (`gh auth status`). Install
  from <https://cli.github.com/>.
- **`python3`** on PATH (used by the fetcher / poster / persist helpers).
- **Node.js 18+** (used by the `@modelcontextprotocol/server-filesystem` MCP
  server).
- **Conductor v0.1.12+** (script agents, `type: workflow` sub-workflows, and
  `reasoning.effort` forwarding).

## Inputs

| Input | Type | Default | Purpose |
|---|---|---|---|
| `pr_url` | string | _required_ | GitHub PR URL (`https://github.com/<owner>/<repo>/pull/<n>`) |
| `focus` | string | `""` | Free-text guidance to direct reviewer attention |
| `exclude` | array | `[]` | Aspects to de-prioritise: `correctness`, `security`, `error_handling`, `performance`, `testing`, `completeness` |
| `enabled_specialists` | array | `[code, security, tests, errors, types, comments]` | Subset of specialists to run |
| `include_polish` | bool | `true` | Run the polish sub-workflow |
| `polish_as_inline` | bool | `false` | Promote polish findings to inline PR comments |
| `strictness` | int | `2` | 1 = critical only, 2 = standard, 3 = thorough |
| `max_rounds` | int | `2` | Maximum deliberation rounds (clamped 1-10) |
| `custom_rules_dir` | string | `""` | Path to a `.pr-review/` directory |
| `guidelines_path` | string | `""` | Path to a single team guidelines markdown file |
| `codebase_context_path` | string | `""` | Path to a precomputed codebase-context markdown |
| `previous_review_path` | string | `""` | Path to a prior `pr-N-review-report.md` for **delta mode** |
| `prior_feedback_path` | string | `""` | JSON of PR-author / maintainer replies to the most recent prior bot review |
| `review_number` | int | `1` | Sequential review number for archive filenames + delta header |
| `output_dir` | string | `""` | Where to write artifacts (defaults to a workflow temp dir) |
| `diff_boundary_nonce` | string | `""` | Pre-generated 8-char hex nonce for `<diff-NONCE>...</diff-NONCE>` markers |
| `auto_post` | bool | `false` | Post the rendered comment + inline comments back to the PR via `gh` |
| `lens_skeptic_model` | string | `claude-opus-4.7` | Model for the Skeptic lens |
| `lens_completeness_model` | string | `gpt-5.5` | Model for the Completeness lens |
| `specialist_*_model` | string | various | Per-specialist model overrides (`specialist_security_model` defaults to `claude-opus-4.7` since security balancing benefits from strong reasoning) |
| `polish_*_model` | string | `claude-sonnet-4.6` | Per-polish-agent model overrides |

## Outputs

| Output | Type | Purpose |
|---|---|---|
| `pr_url`, `pr_number`, `pr_data_dir` | mixed | Echo of the resolved PR + the data dir holding scratch files |
| `review_path` | string | Path to `pr-<N>-review.md` (latest pointer — what the poster posts) |
| `review_report_path` | string | Path to `pr-<N>-review-report.md` (next run reads as `previous_review_path`) |
| `review_archive_path`, `review_report_archive_path` | string | Versioned archives `…-v<review_number>.md` |
| `inline_comments_path` | string | Path to `pr-<N>-inline-comments.json` (or `""` if no inline-able findings) |
| `consensus_reached` | bool | Whether the arbitrator declared consensus |
| `rounds_completed` | int | Deliberation rounds used |
| `merge_readiness` | string | ✅ Ready to merge / ⚠️ Needs minor fixes / ❌ Requires significant rework |
| `confidence_score` | int | 0-100, **review reliability** (not PR quality) |
| `is_delta` | bool | Whether this run produced a delta report |
| `findings_dropped_via_feedback` | int | Delta mode: prior findings dropped because the human reply addressed them |
| `findings_downgraded_via_feedback` | int | Delta mode: severity lowered relative to prior |
| `findings_reraised_with_acknowledgement` | int | Delta mode: re-raised over prior reply |
| `polish_findings_count` | int | Polish stage findings count |
| `posted_comment_url` | string | Set by the poster when `auto_post: true` |
| `status` | string | `"success"` when artifacts were written |

## Examples

### Basic review

```bash
conductor run workflows/pr-review/workflow.yaml \
  --input pr_url="https://github.com/owner/repo/pull/123"
```

### Focused review with auto-post

```bash
conductor run workflows/pr-review/workflow.yaml \
  --input pr_url="https://github.com/owner/repo/pull/123" \
  --input focus="security around the new auth flow" \
  --input strictness=3 \
  --input auto_post=true
```

### Re-review with delta mode

```bash
conductor run workflows/pr-review/workflow.yaml \
  --input pr_url="https://github.com/owner/repo/pull/123" \
  --input previous_review_path="reviews/pr-123-review-report.md" \
  --input prior_feedback_path="reviews/pr-123-prior-feedback.json" \
  --input review_number=2
```

### Cost-controlled run (skip polish, fewer specialists)

```bash
conductor run workflows/pr-review/workflow.yaml \
  --input pr_url="https://github.com/owner/repo/pull/123" \
  --input enabled_specialists='["code","tests","errors"]' \
  --input include_polish=false \
  --input max_rounds=1
```

### Large PR (1M context for the security lens)

```bash
conductor run workflows/pr-review/workflow.yaml \
  --input pr_url="https://github.com/owner/repo/pull/123" \
  --input lens_skeptic_model="claude-opus-4.7-1m-internal"
```

### Debug a single phase (sub-workflow only)

Each sub-workflow can be invoked directly. You'll need to provide the
dispatcher-style inputs manually (or read them from a previous run's
`pr_data_dir/dispatcher-context.json`).

```bash
# Re-run only the deliberation phase against an existing consolidated set
conductor run workflows/pr-review/subworkflows/deliberate.yaml \
  --input pr_data_dir="$PR_DATA_DIR" \
  --input pr_metadata="$(cat "$PR_DATA_DIR/metadata.json")" \
  --input diff_boundary_nonce="$NONCE" \
  --input consolidated_findings="$(cat "$PR_DATA_DIR/consolidated-findings.json")" \
  --input max_rounds=3
```

## What the workflow writes

When complete, the output directory contains:

```
pr-<N>-review.md                         # latest comment, what gets posted
pr-<N>-review-v<review_number>.md        # versioned archive of the comment
pr-<N>-review-report.md                  # latest detailed report (used next run as previous_review_path)
pr-<N>-review-report-v<review_number>.md # versioned archive of the report
pr-<N>-inline-comments.json              # array for inline posting (omitted if empty)
```

The PR data directory also retains intermediate artefacts useful for debugging:

```
metadata.json                   # raw PR metadata from `gh pr view --json`
diff.patch                      # unified diff from `gh pr diff`
changed-files.json              # per-file patches from gh API
prepared-diff.txt               # nonce-wrapped diff that specialists/lenses read
specialist-outputs.json         # raw outputs from each specialist (after the gate)
gate-output.json                # specialist_gate's routing decision
specialist-contributions.json   # consolidator's per-specialist counts
consolidated-findings.json      # the deduped finding set the lenses deliberate over
lens-outputs.json               # raw lens initial dispositions + new findings
deliberation-outputs.json       # per-round lens responses
arbitrator-output.json          # full arbitrator output (consensus + scores)
arbitrator-findings.json        # agreed_findings (legacy file written by arbitrator agent)
polish-outputs.json             # polish_simplifier + polish_dead_code raw findings
review-comment.md               # report_writer's intermediate output
```

## Posting without `auto_post`

If you set `auto_post: false` (the default), this workflow writes the artifacts
above but does NOT post anything. To post manually:

```bash
# Top-level review comment
gh pr comment <pr_number> --repo <owner>/<repo> \
  --body-file "$(jq -r .review_path conductor-output.json)"

# Inline comments (one review with many comments)
INLINE_PATH="$(jq -r .inline_comments_path conductor-output.json)"
HEAD_SHA="$(gh pr view <pr_number> --repo <owner>/<repo> --json headRefOid -q .headRefOid)"
jq --arg sha "$HEAD_SHA" '{
  commit_id: $sha,
  event: "COMMENT",
  body: "Multi-agent PR review — inline findings.",
  comments: [.[] | {path: .file, line: .line, side: "RIGHT", body: .body}]
}' "$INLINE_PATH" \
  | gh api --method POST repos/<owner>/<repo>/pulls/<pr_number>/reviews --input -
```

`scripts/post-review.sh` does exactly this — it's what the optional `poster`
agent invokes when `auto_post: true`.

## Cost controls

The heaviest configuration (5 specialists × 1 round + 2 lenses × 2 rounds + 2
polish agents + writers + persisters) runs ~16 LLM calls and a handful of
script steps. To reduce cost:

- **Trim `enabled_specialists`** to the ones you need. `code` + `security` is
  the strongest two-specialist combo for general PRs.
- **Set `include_polish: false`** for routine PRs.
- **Set `max_rounds: 1`** for low-stakes PRs.
- **Set `strictness: 1`** to report only critical findings.
- **Override per-specialist models** to cheaper options (the defaults already
  put cheaper sonnet-class models on the easier specialists).

## Confidence score formula

The arbitrator (inside the deliberate sub-workflow) emits a `confidence_score`
in `[0, 100]` per:

| Component | Weight | Meaning |
|---|---|---|
| Consensus ratio | 0-40 | `agreed_count / total_findings × 40` |
| Average conviction | 0-30 | `avg_conviction / 10 × 30` across agreed findings |
| Severity coverage | 0-20 | start at 20; subtract 10 per 🔴 critical, 5 per 🟡 warning (floor 0) |
| Specialist convergence | 0-5 | average specialists agreeing per finding (scaled) |
| Efficiency | 0-5 | rounds remaining when consensus reached |

This measures **review reliability**, NOT PR quality. A high score paired with
poor `merge_readiness` reads as: "we are highly confident the PR has serious
problems." `merge_readiness` is the separate, PR-quality verdict.

## Known limitations / roadmap

- **GitHub-only** in v1. ADO support is on the roadmap — the architecture
  is platform-agnostic; only `pr_fetcher` and `poster` need ADO equivalents.
- **No automatic conductor-managed PR thread resolution.** Re-running the
  workflow with `previous_review_path` produces a delta, but resolving prior
  inline-comment threads on the PR side is currently up to the caller.
- **Codebase context, custom rules, and work items** must be precomputed
  by the caller and passed via path inputs. This workflow does not query a
  codebase index or fetch linked issues itself.
