# LogService audit

A repeatable discovery-to-issue workflow for `azure-core-cto/log-service`.
Run it from the LogService checkout, not the workflow registry. It audits a fetched,
pinned default-branch commit in a separate detached worktree; local edits are not
included or overwritten.

**This is an unattended, destructive local-cluster workflow.** Its default mode
rebuilds/deploys the plugin, three LogService slices, and SampleApp, runs tests and
can restart test replicas or processes. `reinstall` additionally replaces the SF
runtime and normally erases cluster data. Use only a disposable development machine.
The defaults publish verified issues automatically. No fixes, commits, pushes, PRs,
comments, or edits to existing issues are made.

## Run

Requirements: an elevated Windows terminal, Conductor with script `stdin`, plugins,
working-directory and checkpoint support, Python 3.12+, Git, authenticated `gh` for
the **origin host**, Windows PowerShell 5.1, the repository's pinned .NET SDK and feed
access, SF prerequisites, and `grpcurl`. The workflow resolves the enterprise host
from `origin`; it does not accidentally publish to the old github.com remote.
The workflow pins its agent settings:

| Step | Model | Context tier | Reasoning |
|---|---|---|---|
| Prepare review (only on blockers) | `claude-sonnet-5` | `default` | `medium` |
| Discover bugs / dead code / stability / performance (four isolated agents) | `gpt-6-astra` | `long_context` | `high` |
| Consolidate | `claude-sonnet-5` | `long_context` | `medium` |
| Reproduce | `claude-opus-5` | `long_context` | `high` |
| Verification | `claude-opus-5` | `long_context` | `xhigh` |
| Candidate cleanup | `claude-sonnet-5` | `default` | `medium` |

Script and termination steps do not use models. Verification is a separate agent,
but uses the same model family as reproduction; evidence gates remain essential.
The UTF-8 settings below avoid Windows console encoding errors when Conductor
prints warning/status symbols.

```powershell
Set-Location C:\src\log-service
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Validate configuration only: no agents, deployment, tests, or issues.
conductor validate ..\conductor-workflows\workflows\log-service-audit\workflow.yaml

# Run with deployment and automatic issue filing.
conductor run ..\conductor-workflows\workflows\log-service-audit\workflow.yaml --web

# Focus a pass and keep issues as local drafts (still deploys and tests).
conductor run ..\conductor-workflows\workflows\log-service-audit\workflow.yaml --web --input publish=false --input focus="Checkpoint recovery and truncation" --input max_candidates=4

# Reinstall the public runtime before deployment and tests; destroys cluster data.
conductor run ..\conductor-workflows\workflows\log-service-audit\workflow.yaml --web --input environment=reinstall
```

For registry use:

```powershell
conductor registry add sample C:\src\conductor-workflows --type path
conductor run log-service-audit@sample --web
```

`environment=existing` skips deployment but still permits disruptive tests. The
agent must prove deployed binary provenance matches the pinned commit before
using live evidence; otherwise those candidates are blocked. It is **not** a
read-only/dry-run mode. Only `conductor validate` is side-effect-free.

## Stages and evidence

| Stage | Responsibility |
|---|---|
| Bootstrap | Validate target/prerequisites, fetch default branch, acquire the machine lock, create isolated worktree, inventory all issues and PRs |
| Preflight | Deterministically assert the toolchain (required executables, Windows PowerShell signature validation, SF runtime/SDK for `existing` mode) and stop before any agent time is spent if it cannot hold |
| Prepare | Run the fixed deployment and baseline sequence as a script, record every invocation, and compute `environment_ready` from observed exit codes |
| Prepare review | Only when the script reports blockers: explain each failure and whether it looks environmental or product-related. It cannot change `environment_ready` |
| Discover | Four read-only agents independently investigate bugs, dead code, stability and performance in parallel; they cannot see sibling outputs |
| Consolidate / queue | Group same-root-cause discoveries, explicitly defer over-budget work, persist the plan and order |
| Candidate sub-workflow | Reproduce one candidate, rank existing issues against it, verify and deduplicate it if validated, publish if approved, then restore state and pass cleanup before starting another |
| Finish | Persist report, release machine lock, retain worktree and evidence |

`workflow.yaml` owns preparation and discovery. Its `for_each` invokes
`candidate.yaml` with **`max_concurrent: 1` and `failure_mode: fail_fast`**.
Each candidate gets separate reproduction, verification and cleanup agent sessions.
Reproduction rejection/blocking skips verification/publication, but never cleanup.
Verification rejection/blocking/duplication also continues through cleanup.
Only successful cleanup marks a candidate completed and permits the next one.
The helper also enforces this ordering independently of Conductor's scheduling.

**Failure isolation.** A stage that fails while leaving the machine intact — a
refused publication gate, a failed inventory refresh, a reproduction that errored
— costs one candidate, not the run. It is recorded as `blocked`, cleanup still
restores the environment, and the next candidate proceeds; the block appears in
the final report. Only `cleanup`/`complete` failing is fatal, because then the
environment itself is unproven and later candidates cannot be trusted.

**Context is explicit.** Both workflows declare `context.mode: explicit` and every
agent lists the inputs it reads, so a long reproduction transcript is not resent to
every later step. Prompts reference `bootstrap.output.summary` rather than the raw
script output: a script step's own stdout is merged into its output, so rendering
that output whole would ship the same JSON twice in every prompt, on every turn.

Each discovery agent may propose up to `max_candidates`; consolidation selects at
most that many candidates in total. Every B/D/S discovery must map to a C candidate
or an explicit deferral with a reason. Several discoveries can map to one root cause.
Deferred hypotheses remain in the report for later runs; they are not silently lost
or presented as validated findings. Discovery/consolidation never execute experiments.

Bug and stability candidates require a passing positive control and two actual
failing reproductions. Failures must assert the claimed defect, not a missing
dependency, compilation error, stale DLL, unrelated flaky meter, or auth failure.
Stability findings require a measurable invariant/threshold, not a general suggestion
to improve code style.

Performance candidates follow a different rule, because a measurement run exits 0
even when it records a regression — there is no failing assertion to point at. They
require a recorded reference measurement, at least two successful measurement runs,
and the numbers themselves: metric, unit, scenario, reference value, observed value,
samples per side, and the threshold being judged against. A single sample, or a
difference inside run-to-run noise, is rejected rather than published. Meaningful
end-to-end numbers need a healthy cluster, so these are blocked rather than
substituted with a micro-benchmark when `environment_ready` is false. Published
performance issues carry a measurement table with the computed change.

Dead-code candidates require static **and dynamic** reachability analysis, a passing
baseline, a saved deletion patch, and passing affected build/tests with the deletion
present. Public compatibility, reflection, serialization, DI, SF activation, build
conditions and plugin consumers must be considered. Uncertain reachability blocks
publication, even when removal passes tests.

The recorder stores exact argv, working directory, commit, timestamps, exit status,
stdout/stderr, pre-execution tracked diff, candidate ID and local/cluster scope.
The publisher rejects proof records from another candidate and verifies artifact
hashes and proof shape. The independent agent verifies their **meaning**; exit codes alone
are not proof. These agents have shell access, so this is not a security sandbox or
cryptographic attestation against a malicious agent.

A killed, timed-out recorder execution can still exit nonzero (or, rarely, zero) by
accident, so it can never itself satisfy a passing baseline/control/build/test stage
or count toward the two required failing reproductions. A superseded timed-out
attempt may still be listed in a result's evidence for transparency alongside the
attempt that actually demonstrates the defect; it is simply inert for every gate.

The workflow preserves these LogService-specific requirements:

- Install public runtime only with `Install-PublicBuild.ps1`; never hot-swap a local
  WindowsFabric build during this audit.
- Deploy the runtime plugin separately and compare built/installed DLL hashes.
- Deploy all three slices; respect per-tenant identities and mTLS sessions.
- Build/deploy the E2E harness before smoke's `-SkipDeploy` E2E call.
- Run smoke with `-SkipWfBuild -SkipLogServiceDeploy` after setup.
- Use Windows PowerShell 5.1 and check every child command's actual result.

Preparation failures stop dependent live tests, not unrelated source inspection or
in-process proofs. Every candidate receives a disposition; blocked work and untested
areas remain visible. A completed workflow is **not** a claim of exhaustive coverage
or a healthy cluster. Inspect `environment_ready`, blockers and coverage in the report.

## Duplicate handling and publication

The verifier reads a complete paginated inventory of **all** open/closed issues and
PRs, not just audit-labelled records. A deterministic pre-ranking step scores every
inventory record against the candidate's symbol, path, invariant and title, and
hands the agent that shortlist plus the count it covers and the path to the full
file — ranking is lexical, so the agent is told to read promising entries in full
and widen the search whenever cause or symptom suggests a match the wording missed.
It compares cause, symbol, invariant and symptom,
and compares earlier approved candidate reports (including unpublished drafts).
The inventory is refreshed for each validated candidate, so issues created earlier
in the run are included. Identical earlier finding fingerprints are also suppressed
deterministically. Closed matches remain
duplicates; this workflow neither reopens them nor files a new "regression" issue
without human triage.

Issue bodies include a stable hash marker derived from source path, symbol and
invariant (not SHA or line numbers). The publisher checks that marker again immediately
before creation, and refuses to create anything if a new issue was filed since semantic
review — unrelated state changes, edits and pull-request churn no longer block it,
because they cannot introduce a duplicate the reviewer has not seen.
It verifies the resulting issue has the marker and `needs triage`.
The label is created if missing, but only during actual publication.

A pending receipt is written **before** issue creation. If GitHub accepts a request
but the response is lost, resume reconciles the marker instead of blindly retrying.
If it cannot determine whether a previous request succeeded, it stops for inspection.
Earlier successful issues remain recorded even if a later publication fails.

The machine lock serializes runs on this host, not every publisher worldwide.
GitHub issue creation has no transactional uniqueness constraint: an unrelated
publisher on another machine can still race, and semantic equivalence needs judgment.
Run only one publishing audit for this repository at a time. The workflow favors
blocking on uncertainty over creating likely duplicates.

## Reports, recovery and cleanup

Artifacts live under
`%LOCALAPPDATA%\Conductor\log-service-audit\runs\<timestamp-id>\`:

- `run.json`, `plan.json`, `issues.json`: pinned environment, immutable consolidated
  discovery plan with all source mappings, and the current reviewed GitHub inventory.
- `records\<name>\`: immutable command records, output files and tracked diffs.
- Reproduction scripts/patches created by the agents.
- `candidates\<id>\`: saved reproduction, reviewed report, drafts, publication
  resolution, cleanup result and completion marker for that candidate.
- `receipts\`, `report.json`: run-wide publication receipts and the aggregate report,
  including pending candidates, all discovery mappings, coverage and blockers.

Before completion, cleanup archives reproduction files and restores only the
candidate's tracked/untracked changes and synthetic test state. The helper requires
the pinned HEAD and a clean worktree. For a candidate that recorded cluster access,
it additionally executes a recorded local SF health check requiring aggregate `Ok`
and every node `Up`. Failed restoration/health checks block subsequent candidates
even if the issue was already published. Local-only work can proceed against a
blocked live baseline, but the recorder refuses cluster access in that state.

Raw logs stay local and may still contain sensitive service output. Do not upload
the directory. Public issue bodies contain self-contained, reviewed reproductions
and a concise evidence table, not raw logs or machine-local-only instructions.

Worktrees live in a sibling `log-service.worktrees\audit-<timestamp-id>` directory.
They are deliberately retained for investigation, including after success. Remove
only a specific inspected audit worktree with `git worktree remove <exact-path>`
when its reproduction artifacts are no longer needed; never bulk-delete worktrees.

The machine-wide lock is
`%PROGRAMDATA%\Conductor\log-service-audit\cluster.lock\owner.json`.
Unexpected failures retain it, and no stale-lock timeout silently steals it.
Inspect its owner, Conductor status, and recorded test processes before recovery.
Do not start another audit while an old deployment/test is alive.

For provider/interruption failures, use Conductor's checkpoint/resume support with
the same workflow and preserve the run directory. Explicit script-failure termination
does not preserve Conductor checkpoints. Once discovery has been queued, the plan
and per-candidate progress permit recovery without replaying completed candidates:

```powershell
conductor run ..\conductor-workflows\workflows\log-service-audit\workflow.yaml --web --input recover_run="C:\Users\<user>\AppData\Local\Conductor\log-service-audit\runs\<timestamp-id>"
```

Recovery requires the retained lock and pinned worktree. It skips preparation,
discovery and completed candidates. An unfinished candidate without saved reproduction
is reproduced; one with saved reproduction resumes review with a fresh inventory;
one with a saved publication resolution resumes cleanup only. It then processes
remaining candidates in order. Other inputs are ignored; original settings remain
in force. Record names must be unique, even when repeating a failed attempt.
Do not edit snapshots or approve stale decisions by hand. Inspect pending receipts
on GitHub before recovery. When the create outcome remains ambiguous and no marker
is found, recovery still stops rather than retrying.
Receipt reconciliation also runs when renewed verification calls the finding a
duplicate; that verdict cannot bypass checking a pending creation or its label.

### Republishing approved findings whose publication failed

A finding can be verified and approved yet still fail to publish — a dropped
connection, or a duplicate re-check that a concurrently filed issue invalidates.
The candidate is recorded `blocked` with its evidence intact, so publication can
be replayed without re-running any experiment:

```powershell
python audit.py republish --run-dir "<run directory>"
```

This re-acquires the machine lock, reconciles pending receipts, refreshes the
issue snapshot, and publishes every candidate that is `blocked` with an `approved`
verification. It refuses to run while another audit holds the lock, and it stops
if any issue filed since the run began is not one of this run's own publications.
Compare each such issue against the pending findings and record the review
explicitly:

```powershell
python audit.py republish --run-dir "<run directory>" --reviewed 215
```

Pending receipts are cleared only when two inventory reads, separated by a
settling delay, both show the issue's marker absent — so a create whose response
was lost is never duplicated. `python audit.py reconcile --run-dir "<dir>"` runs
that step alone. Read-only `gh` calls retry transient network faults; issue
creation never does, because the receipt is what makes it safe.

Finalization persists its result before releasing the lock and is safe to replay.
It atomically moves an owned lock to a run-specific release directory before removing
it. Recovery of an already completed audit returns its saved result and cleans up only
that run's remaining lock artifacts, never a newer audit's lock.

An interrupted recorder can leave `candidates\<id>\inflight.json` with command/PID
details. Recovery refuses to proceed while it exists. An operator must verify that
the recorded process and its descendants have stopped and inspect their effects
before retiring that exact inflight marker. Agents must not clear it themselves.
Failure before a plan exists requires a usable Conductor checkpoint or explicitly
retiring the failed run and starting over.

To retire a stopped/failed run, first confirm its processes are stopped and preserve
the evidence. Remove only that lock's `owner.json`, then the empty `cluster.lock`
directory. No automated cleanup deletes cluster data, evidence, or worktrees.

## Offline tests

Run these with a Python environment containing Jinja2 and PyYAML. Conductor installs
both, but an isolated Conductor installation may use a different Python environment
from the `python` command on PATH.

```powershell
python -m unittest discover -s ..\conductor-workflows\workflows\log-service-audit\tests -v
```

Tests execute harmless local recorder commands and mock GitHub; they never deploy,
connect to SF, or create issues. They cover proof gating, missing/tampered evidence,
duplicate suppression, stale inventory, ambiguous create outcomes, label verification,
dry-run behavior and workflow routing. They do not replace a live end-to-end audit.
They also cover discovery accounting, serial candidate ordering, cleanup/health
gates, per-candidate evidence ownership, and skipping completed work on recovery.
