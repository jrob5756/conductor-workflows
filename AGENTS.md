# Agent Instructions

Guidance for AI coding agents working in this repository.

## Repository overview

This is a sample workflow registry for [Conductor](https://github.com/microsoft/conductor), and a plugin marketplace. It contains:

- `index.yaml` — registry index listing available workflows
- `workflows/` — workflow definitions (YAML)
- `.github/plugin/marketplace.json` — plugin marketplace registry
- `plugins/` — plugins exposing workflows as skills
- `README.md` — user-facing documentation

## Conventions

- Workflow files live under `workflows/` and are referenced from `index.yaml`.
- Keep `README.md` in sync with `index.yaml` when adding, removing, or renaming workflows.
- Use lowercase, hyphen-separated names for workflow ids (e.g., `sdd-design`).
- Prefer YAML over JSON for workflow definitions.

## Plugins and skills

- Plugins live at `plugins/<plugin>/` with a `.github/plugin/plugin.json` manifest (Copilot convention) and are registered in `.github/plugin/marketplace.json`.
- Skills live at `plugins/<plugin>/skills/<skill>/SKILL.md`. The `name` in the frontmatter **must** equal the parent directory name, or the skill silently fails to load. Names are kebab-case and must not contain `/`, `.`, or `:`.
- The plugin/skill pair determines the invocation: `plugins/workflows/skills/fusion` is invoked as `/workflows:fusion`.
- **Skills must not bundle copies of workflow YAML.** Resolve workflows through the Conductor registry (`conductor run <id>@<registry>`) so a skill and its workflow cannot drift apart.
- A skill that wraps a workflow must never answer the user's request itself when the workflow fails — the orchestration is the point, so it reports the error and stops.

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
