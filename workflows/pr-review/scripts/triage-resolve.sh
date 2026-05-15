#!/usr/bin/env bash
#
# triage-resolve.sh — Combine heuristic + LLM triage outputs with user inputs
# to produce the final list of specialists that will actually run.
#
# Inputs (positional, all JSON strings or scalar):
#   $1  triage_mode            — "auto" | "all"
#   $2  enabled_specialists    — JSON array (e.g. '["code","security",...]')
#   $3  force_specialists      — JSON array (e.g. '["security"]') — bypass triage
#   $4  heuristic_json         — full heuristic.output JSON object
#   $5  llm_json               — full triage_llm.output JSON object (or "{}")
#   $6  pr_data_dir            — where to write the persisted triage.json
#
# Outputs (stdout, JSON object — conductor auto-merges):
#   {
#     "recommended_specialists": [ ... ],
#     "skipped_specialists":     [ ... ],
#     "triage_mode":             "auto",
#     "rationale":               "human-readable explanation",
#     "triage_persisted_path":   "<pr_data_dir>/triage.json",
#     "resolver_status":         "success"
#   }

set -euo pipefail

TRIAGE_MODE="${1:-auto}"
ENABLED_JSON="${2:-[]}"
FORCE_JSON="${3:-[]}"
# Note: bash chokes on ${4:-{}} because the literal `{}` collides with parameter
# expansion syntax. Use empty default + explicit fallback below instead.
HEURISTIC_JSON="${4:-}"
LLM_JSON="${5:-}"
PR_DATA_DIR="${6:-}"

if [[ -z "$HEURISTIC_JSON" ]]; then HEURISTIC_JSON='{}'; fi
if [[ -z "$LLM_JSON" ]];       then LLM_JSON='{}';       fi

if [[ -z "$PR_DATA_DIR" ]]; then
  echo '{"error": "triage-resolve.sh requires pr_data_dir as 6th argument"}' >&2
  exit 1
fi

mkdir -p "$PR_DATA_DIR"

python3 - "$TRIAGE_MODE" "$ENABLED_JSON" "$FORCE_JSON" "$HEURISTIC_JSON" "$LLM_JSON" "$PR_DATA_DIR" <<'PY'
import json
import os
import sys

triage_mode, enabled_raw, force_raw, heuristic_raw, llm_raw, pr_data_dir = sys.argv[1:7]

# Canonical specialist list — keep in sync with subworkflows/specialists.yaml.
ALL_SPECIALISTS = ["code", "security", "tests", "errors", "types", "comments"]

def parse_json(name: str, raw: str, default):
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"triage-resolve: bad {name} JSON: {e}"}), file=sys.stderr)
        sys.exit(2)

enabled = parse_json("enabled_specialists", enabled_raw, list(ALL_SPECIALISTS))
force = parse_json("force_specialists", force_raw, [])
heuristic = parse_json("heuristic_json", heuristic_raw, {})
llm = parse_json("llm_json", llm_raw, {})

# Validate scalar string inputs.
if not isinstance(enabled, list):
    enabled = list(ALL_SPECIALISTS)
if not isinstance(force, list):
    force = []
if triage_mode not in {"auto", "all"}:
    triage_mode = "auto"

# Filter to known names.
enabled = [s for s in enabled if s in ALL_SPECIALISTS]
force = [s for s in force if s in ALL_SPECIALISTS]
if not enabled:
    enabled = list(ALL_SPECIALISTS)

heuristic_candidates = heuristic.get("candidates") or []
llm_recommended = llm.get("recommended") or []
llm_reasoning = (llm.get("reasoning") or "").strip()
heuristic_rationale = (heuristic.get("rationale") or "").strip()
trivial = bool(heuristic.get("trivial_docs_only"))

rationale_parts: list[str] = []

if triage_mode == "all":
    recommended = list(enabled)
    rationale_parts.append("triage_mode=all → running all enabled specialists, skipping triage gate.")
else:
    # auto mode: union of heuristic + LLM, intersected with enabled, then unioned with force.
    triage_union = list({*heuristic_candidates, *llm_recommended})
    gated = [s for s in enabled if s in triage_union]
    recommended = list({*gated, *force})

    if trivial:
        rationale_parts.append(heuristic_rationale or "Heuristic flagged trivial docs-only PR.")
    else:
        if heuristic_rationale:
            rationale_parts.append(f"Heuristic: {heuristic_rationale}")
        if llm_reasoning:
            rationale_parts.append(f"LLM: {llm_reasoning}")
        if force:
            rationale_parts.append(f"Force-enabled (bypassing triage): {', '.join(force)}.")

# Safety net — never return an empty list. Fall back to "code".
if not recommended:
    recommended = ["code"]
    rationale_parts.append("Safety net: no specialists matched; defaulting to code.")

# Stable order — match ALL_SPECIALISTS so persisted output is deterministic.
recommended = [s for s in ALL_SPECIALISTS if s in recommended]
skipped = [s for s in ALL_SPECIALISTS if s in enabled and s not in recommended]

result = {
    "recommended_specialists": recommended,
    "skipped_specialists": skipped,
    "triage_mode": triage_mode,
    "enabled_specialists": enabled,
    "force_specialists": force,
    "heuristic_candidates": heuristic_candidates,
    "llm_recommended": llm_recommended,
    "trivial_docs_only": trivial,
    "rationale": " ".join(rationale_parts).strip(),
}

# Persist a richer record for the render sub-workflow / debugging archives.
target_path = os.path.join(pr_data_dir, "triage.json")
persisted = dict(result)
persisted["heuristic"] = heuristic
persisted["llm"] = llm
with open(target_path, "w") as f:
    json.dump(persisted, f, indent=2, ensure_ascii=False)

result["triage_persisted_path"] = target_path
result["resolver_status"] = "success"

print(json.dumps(result, ensure_ascii=False))
PY
