# Conductor Workflows

Sample workflow registry for [Conductor](https://github.com/microsoft/conductor), plus a plugin marketplace that exposes workflows as skills.

## Workflows

| Name | Description |
|------|-------------|
| `document-create` | Create a new markdown document grounded in the codebase, with a structure inferred from the stated purpose, with technical and readability review cycles (loops back to the author until both thresholds are met) |
| `document-review` | Review-only scoring of a markdown document on technical accuracy and readability, with threshold short-circuit |
| `document-update` | Update an existing markdown document to incorporate a stated purpose, with technical and readability review cycles (loops back to the editor until both thresholds are met) |
| `fusion` | Multi-model deliberation modelled on OpenRouter's Fusion Router: a panel of models from different labs answers independently in parallel, an analyst compares (never merges) their answers into consensus, contradictions, coverage gaps, unique insights and blind spots, and a synthesiser writes the final answer. A cheap gate lets trivial questions skip the panel |
| `log-service-audit` | [Evidence-gated LogService audit](workflows/log-service-audit/README.md): isolated bug/dead-code/stability discovery, duplicate consolidation, then one candidate at a time through reproduction, verification, `needs triage` issue creation and cleanup |
| `pr-review` | Review open pull requests for repository fit and implementation quality in isolated worktrees. Reuse passing CI, approve or retry runs only when needed, and reconcile current checks without duplicate findings. Follow-up runs track distinct earlier findings. Validate approved finding coverage and the reviewed head before publishing. Approval and squash-merge require separate decisions; stopped runs remain resumable until explicitly discarded |
| `sdd-design` | Solution design document with technical and readability review cycles, a fixer agent applying targeted revisions between rounds, and a human gate when reviews don't converge (no implementation plan) |
| `sdd-plan` | Solution design + implementation plan with technical and readability review cycles, a fixer agent applying targeted revisions between rounds, and a human gate when reviews don't converge. Optional `design` input switches it to plan-only mode, consuming an existing design document (e.g. one produced by `sdd-design`) |
| `sdd-implement` | Implement a plan epic-by-epic with epic-level and plan-level review |
| `ship` | Take an existing GitHub issue to a merged pull request: cuts a worktree, plans behind a human question gate, implements unattended, opens a draft PR, reviews it and applies the findings in a separate step, then publishes and merges behind a human gate with full branch and worktree cleanup. A merge blocked by conflicts pauses to ask whether to resolve them, and `autopilot=true` bypasses every human gate |

## Usage

```bash
# Add this registry
conductor registry add sample /path/to/conductor-workflows --default

# List workflows
conductor registry list sample

# Run a workflow
conductor run sdd-plan --input goal="Design a caching layer"
```

### PR review behavior

Run `pr-review` from the repository containing an open pull request. Closed
and merged pull requests stop before review. Each invocation owns a separate
worktree; starting another review does not remove an earlier checkout.

CI startup distinguishes passing results reused, runs already in progress,
fork approvals, and retries of failed runs. The workflow still inspects fresh
results after review. Missing Actions runs trigger bounded rediscovery, not
an immediate failure or an assumption that no CI exists. External checks and
required checks are inspected too. If enabled workflows exist but their
applicability cannot be established, the workflow reports that uncertainty
rather than claiming CI passed.

Findings retain identities through triage, wording, and publication. Current
CI results supersede earlier CI assessments; review summaries do not count as
additional findings when their relationship to inline findings is accounted
for. Publication rejects missing or duplicate approved findings and checks
that the pull request is still open at the reviewed head immediately before
posting. A changed head requires a new review.

Stopping through the dashboard retains the checkpoint and worktree for
resumption. It is not the same as choosing **Post nothing**, which follows the
workflow's cleanup path. Keep retained worktrees until you resume or explicitly
discard their runs; discarding a worktree makes checkpoints depending on it
unusable. Cleanup warnings identify resources that could not safely be removed.

To discard a retained run, first stop its runner/dashboard and do not resume it
concurrently. From this registry checkout, supply the exact worktree path
reported by the run:

```bash
python3 workflows/pr-review/scripts/discard.py \
  /path/to/target-repository '/path/to/target-repository.worktrees/pr-123-<id>' \
  --acknowledge-discard
```

Discard verifies ownership, the reviewed head, and runner liveness. It refuses
active or uncertain owners and legacy worktrees without ownership records.
Add `--allow-dirty` only when you also intend to delete uncommitted, untracked,
and ignored files. It does not override ownership or changed-head checks.

The owned-worktree lifecycle currently requires Linux `/proc` and `flock`.
Automatic discard cannot verify ownership across boots or PID namespaces and
refuses those cases; inspect retained resources manually rather than bypassing
the checks. A still-live dashboard is conservatively treated as an active owner.

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
