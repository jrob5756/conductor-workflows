# Agent Instructions

Guidance for AI coding agents working in this repository.

## Repository overview

This is a sample workflow registry for [Conductor](https://github.com/microsoft/conductor). It contains:

- `index.yaml` — registry index listing available workflows
- `workflows/` — workflow definitions (YAML)
- `README.md` — user-facing documentation

## Conventions

- Workflow files live under `workflows/` and are referenced from `index.yaml`.
- Keep `README.md` in sync with `index.yaml` when adding, removing, or renaming workflows.
- Use lowercase, hyphen-separated names for workflow ids (e.g., `sdd-design`).
- Prefer YAML over JSON for workflow definitions.

## Validating changes

When modifying workflows:

1. Run `conductor validate <path-to-workflow.yaml>` for **every** workflow you change. This catches schema, routing, agent-reference, and template errors before commit.
2. If you add, remove, or rename a workflow, validate every workflow listed in `index.yaml` to confirm nothing else regressed.
3. Confirm the workflow id in the file matches its entry in `index.yaml`.
4. Update `README.md` if the public-facing list or description changes.

CI runs `conductor validate` on every YAML under `workflows/` for each pull request (`.github/workflows/validate-workflows.yml`). Passing locally is a prerequisite for opening a PR.

## Commit guidance

- Make focused, surgical changes.
- Do not commit secrets or local-only configuration.
