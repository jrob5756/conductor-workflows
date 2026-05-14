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
- Use lowercase, hyphen-separated names for workflow ids (e.g., `sdd-plan-v3`).
- Prefer YAML over JSON for workflow definitions.

## Validating changes

When modifying workflows:

1. Ensure YAML is valid.
2. Confirm the workflow id in the file matches its entry in `index.yaml`.
3. Update `README.md` if the public-facing list or description changes.

## Commit guidance

- Make focused, surgical changes.
- Do not commit secrets or local-only configuration.
