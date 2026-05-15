#!/usr/bin/env python3
"""triage_resolve.py — Combine heuristic + LLM triage outputs with user inputs.

Produces the final list of specialists that will actually run.

Inputs (positional, all JSON strings or scalar):
    1  triage_mode            — "auto" | "all"
    2  enabled_specialists    — JSON array (e.g. '["code","security",...]')
    3  force_specialists      — JSON array (e.g. '["security"]') — bypass triage
    4  heuristic_json         — full heuristic.output JSON object
    5  llm_json               — full triage_llm.output JSON object (or "{}")
    6  pr_data_dir            — where to write the persisted triage.json

Outputs (stdout, JSON object — conductor auto-merges):
    {
        "recommended_specialists": [ ... ],
        "skipped_specialists":     [ ... ],
        "triage_mode":             "auto",
        "rationale":               "human-readable explanation",
        "triage_persisted_path":   "<pr_data_dir>/triage.json",
        "resolver_status":         "success"
    }

Exit codes:
    0  success
    1  missing pr_data_dir
    2  invalid JSON in any of the JSON-string args
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Canonical specialist list — keep in sync with subworkflows/specialists.yaml.
ALL_SPECIALISTS = ["code", "security", "tests", "errors", "types", "comments"]


def parse_json(name: str, raw: str, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(
            json.dumps({"error": f"triage_resolve: bad {name} JSON: {e}"}),
            file=sys.stderr,
        )
        sys.exit(2)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("triage_mode", nargs="?", default="auto")
    parser.add_argument("enabled_specialists_json", nargs="?", default="[]")
    parser.add_argument("force_specialists_json", nargs="?", default="[]")
    parser.add_argument("heuristic_json", nargs="?", default="{}")
    parser.add_argument("llm_json", nargs="?", default="{}")
    parser.add_argument("pr_data_dir", nargs="?", default="")
    args = parser.parse_args(argv)

    if not args.pr_data_dir:
        print(
            json.dumps(
                {"error": "triage_resolve.py requires pr_data_dir as 6th argument"}
            ),
            file=sys.stderr,
        )
        return 1

    pr_data_dir = Path(args.pr_data_dir)
    pr_data_dir.mkdir(parents=True, exist_ok=True)

    triage_mode = args.triage_mode
    enabled = parse_json(
        "enabled_specialists", args.enabled_specialists_json, list(ALL_SPECIALISTS)
    )
    force = parse_json("force_specialists", args.force_specialists_json, [])
    heuristic = parse_json("heuristic_json", args.heuristic_json, {})
    llm = parse_json("llm_json", args.llm_json, {})

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
        rationale_parts.append(
            "triage_mode=all → running all enabled specialists, skipping triage gate."
        )
    else:
        # auto mode: union of heuristic + LLM, intersected with enabled, then
        # unioned with force.
        triage_union = list({*heuristic_candidates, *llm_recommended})
        gated = [s for s in enabled if s in triage_union]
        recommended = list({*gated, *force})

        if trivial:
            rationale_parts.append(
                heuristic_rationale or "Heuristic flagged trivial docs-only PR."
            )
        else:
            if heuristic_rationale:
                rationale_parts.append(f"Heuristic: {heuristic_rationale}")
            if llm_reasoning:
                rationale_parts.append(f"LLM: {llm_reasoning}")
            if force:
                rationale_parts.append(
                    f"Force-enabled (bypassing triage): {', '.join(force)}."
                )

    # Safety net — never return an empty list. Fall back to "code".
    if not recommended:
        recommended = ["code"]
        rationale_parts.append("Safety net: no specialists matched; defaulting to code.")

    # Stable order — match ALL_SPECIALISTS so persisted output is deterministic.
    recommended = [s for s in ALL_SPECIALISTS if s in recommended]
    skipped = [s for s in ALL_SPECIALISTS if s in enabled and s not in recommended]

    result: dict[str, Any] = {
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
    target_path = pr_data_dir / "triage.json"
    persisted = dict(result)
    persisted["heuristic"] = heuristic
    persisted["llm"] = llm
    with target_path.open("w", encoding="utf-8") as f:
        json.dump(persisted, f, indent=2, ensure_ascii=False)

    result["triage_persisted_path"] = str(target_path)
    result["resolver_status"] = "success"

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
