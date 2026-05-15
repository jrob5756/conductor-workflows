#!/usr/bin/env bash
#
# triage-heuristic.sh — Classify PR's changed files into specialist buckets.
#
# Purely deterministic — no LLM call. Reads <pr_data_dir>/changed-files.json,
# buckets each file by extension/path, and recommends an initial set of
# specialists. The follow-up triage_llm step refines this; the resolver
# combines both with the user's enabled_specialists/force_specialists.
#
# Usage:
#   triage-heuristic.sh <pr_data_dir>
#
# Output (stdout, JSON object — conductor auto-merges into agent.output.*):
#   {
#     "trivial_docs_only": bool,         // true → llm step can skip
#     "total_files": int,
#     "buckets": {
#       "docs":               [ "README.md", ... ],
#       "tests":              [ "src/foo.test.ts", ... ],
#       "security_sensitive": [ "package.json", "Dockerfile", ... ],
#       "types_eligible":     [ "src/foo.ts", ... ],
#       "source":             [ "src/foo.py", ... ],
#       "config":             [ ".eslintrc", "tsconfig.json", ... ],
#       "binary":             [ "logo.png", ... ]
#     },
#     "candidates":          [ "code", "tests", ... ],   // initial recommendation
#     "rationale":           "string explanation",
#     "heuristic_status":    "success"
#   }

set -euo pipefail

PR_DATA_DIR="${1:-}"

if [[ -z "$PR_DATA_DIR" ]]; then
  echo '{"error": "triage-heuristic.sh requires <pr_data_dir>"}' >&2
  exit 1
fi

CHANGED_FILES="$PR_DATA_DIR/changed-files.json"

if [[ ! -f "$CHANGED_FILES" ]]; then
  echo "{\"error\": \"changed-files.json not found at $CHANGED_FILES\"}" >&2
  exit 2
fi

python3 - "$CHANGED_FILES" <<'PY'
import json
import sys
import os
from fnmatch import fnmatch

changed_path = sys.argv[1]
with open(changed_path) as f:
    raw = json.load(f)

# Normalise to a flat list of file objects. gh api --paginate concatenates
# pages into one array; a stray dict (single page) is also tolerated.
if isinstance(raw, dict):
    files = [raw]
elif isinstance(raw, list):
    files = raw
else:
    print(json.dumps({"error": "changed-files.json has unexpected shape"}), file=sys.stderr)
    sys.exit(3)

# ---------------------------------------------------------------------------
# Classification rules (in priority order — first match wins per bucket).
# A file may live in multiple buckets (e.g. tests AND types_eligible) so we
# keep the lookup per-bucket rather than collapsing to a single label.
# ---------------------------------------------------------------------------

DOC_PATTERNS = [
    "*.md", "*.mdx", "*.rst", "*.txt", "*.adoc",
    "docs/*", "doc/*", "*/docs/*", "*/doc/*",
    "AUTHORS*", "CONTRIBUTORS*", "LICENSE*", "NOTICE*", "CODEOWNERS",
]

TEST_PATTERNS = [
    "*.test.*", "*.spec.*", "*_test.*", "*_spec.*",
    "*/test/*", "*/tests/*", "*/__tests__/*", "*/spec/*",
    "test/*", "tests/*", "__tests__/*", "spec/*",
    "conftest.py", "*test_*.py",
]

SECURITY_SENSITIVE_PATTERNS = [
    "Dockerfile*", "*.dockerfile", "docker-compose*.yml", "docker-compose*.yaml",
    ".github/workflows/*", ".github/actions/*",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "requirements*.txt", "pyproject.toml", "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock",
    "go.mod", "go.sum",
    "Cargo.toml", "Cargo.lock",
    "Gemfile", "Gemfile.lock",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle*",
    "composer.json", "composer.lock",
    "*.csproj", "packages.lock.json", "Directory.Build.props",
    ".env*", "*.pem", "*.key", "*.crt", "*.cert", "*.pfx",
    "*/auth/*", "*/security/*", "*/crypto/*",
]

TYPES_PATTERNS = [
    "*.ts", "*.tsx", "*.d.ts", "*.mts", "*.cts",
]

CONFIG_PATTERNS = [
    "*.json", "*.yml", "*.yaml", "*.toml", "*.ini", "*.cfg",
    ".editorconfig", ".gitignore", ".gitattributes", ".prettierrc*",
    ".eslintrc*", "tsconfig*.json", "*.config.js", "*.config.ts", "*.config.mjs",
]

BINARY_PATTERNS = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.bmp", "*.tiff", "*.webp",
    "*.pdf", "*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.7z", "*.rar",
    "*.woff", "*.woff2", "*.ttf", "*.eot", "*.otf",
    "*.so", "*.dylib", "*.dll", "*.exe", "*.bin",
    "*.mp3", "*.mp4", "*.mov", "*.avi", "*.wav",
]

def matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch(path, pat) for pat in patterns)

def is_test(path: str) -> bool:
    base = os.path.basename(path)
    if base in {"conftest.py"}:
        return True
    return matches_any(path, TEST_PATTERNS)

def is_doc(path: str) -> bool:
    return matches_any(path, DOC_PATTERNS)

def is_binary(path: str) -> bool:
    return matches_any(path, BINARY_PATTERNS)

def is_config(path: str) -> bool:
    return matches_any(path, CONFIG_PATTERNS)

def is_security_sensitive(path: str) -> bool:
    return matches_any(path, SECURITY_SENSITIVE_PATTERNS)

def is_types_eligible(path: str) -> bool:
    return matches_any(path, TYPES_PATTERNS)

def is_source(path: str, doc: bool, test: bool, binary: bool, config: bool) -> bool:
    if doc or binary:
        return False
    # Treat tests as source too — they still benefit from the code specialist.
    # Treat config as non-source (configs trigger security but not code review).
    if config and not test:
        return False
    return True

# ---------------------------------------------------------------------------
# Walk files.
# ---------------------------------------------------------------------------

buckets = {
    "docs": [],
    "tests": [],
    "security_sensitive": [],
    "types_eligible": [],
    "source": [],
    "config": [],
    "binary": [],
}

active_files = 0  # excludes removed files; those carry no review value.

for entry in files:
    path = entry.get("filename") or entry.get("path") or ""
    if not path:
        continue
    status = (entry.get("status") or "").lower()
    if status == "removed":
        # Removed files don't need specialist review — only the absence matters,
        # which the lens reviewers will surface from the diff.
        continue
    active_files += 1

    doc = is_doc(path)
    test = is_test(path)
    binary = is_binary(path)
    config = is_config(path)
    sec = is_security_sensitive(path)
    typ = is_types_eligible(path)
    src = is_source(path, doc, test, binary, config)

    if doc:    buckets["docs"].append(path)
    if test:   buckets["tests"].append(path)
    if binary: buckets["binary"].append(path)
    if config: buckets["config"].append(path)
    if sec:    buckets["security_sensitive"].append(path)
    if typ:    buckets["types_eligible"].append(path)
    if src:    buckets["source"].append(path)

# ---------------------------------------------------------------------------
# Recommendation logic.
# ---------------------------------------------------------------------------

candidates: list[str] = []
rationale_parts: list[str] = []

trivial_docs_only = (
    active_files > 0
    and len(buckets["docs"]) == active_files
    and not buckets["source"]
    and not buckets["security_sensitive"]
    and not buckets["tests"]
    and not buckets["types_eligible"]
)

if trivial_docs_only:
    candidates = ["comments"]
    rationale_parts.append(
        f"Docs-only PR ({len(buckets['docs'])} doc file(s)) — only the comments specialist runs."
    )
else:
    # Source / config / sec changes warrant the code specialist.
    if buckets["source"] or buckets["security_sensitive"] or buckets["config"]:
        candidates.append("code")
        if buckets["source"]:
            rationale_parts.append(f"Source changes in {len(buckets['source'])} file(s) → code.")
        elif buckets["config"] or buckets["security_sensitive"]:
            rationale_parts.append("Config/dependency changes → code (general review).")

    # Tests bucket — also fire if source changed but no tests touched (gap detection).
    if buckets["tests"]:
        candidates.append("tests")
        rationale_parts.append(f"Test files changed ({len(buckets['tests'])}) → tests.")
    elif buckets["source"]:
        candidates.append("tests")
        rationale_parts.append(
            f"Source changed ({len(buckets['source'])} file(s)) without test changes → "
            "tests (gap detection)."
        )

    # Errors / silent-failure analysis applies to any non-trivial source change.
    if buckets["source"]:
        candidates.append("errors")
        rationale_parts.append("Source changes → errors (silent-failure hunt).")

    # Type design only applies when TS/TSX is touched.
    if buckets["types_eligible"]:
        candidates.append("types")
        rationale_parts.append(f"TypeScript files changed ({len(buckets['types_eligible'])}) → types.")

    # Security: lockfiles / dockerfiles / workflows / auth-pathed source / new public
    # endpoints. The heuristic is conservative — the LLM step refines further (e.g. it
    # spots "this Python diff adds an unauthenticated endpoint").
    if buckets["security_sensitive"]:
        candidates.append("security")
        rationale_parts.append(
            f"Security-sensitive files touched ({len(buckets['security_sensitive'])}) → security."
        )

    # Comments review — useful any time docs OR source comments may have rotted.
    # Always run when docs changed; for source-only PRs the LLM step decides.
    if buckets["docs"]:
        candidates.append("comments")
        rationale_parts.append(f"Docs changed ({len(buckets['docs'])}) → comments.")

# Dedupe while preserving insertion order.
seen = set()
candidates = [c for c in candidates if not (c in seen or seen.add(c))]

# Edge-case safety net — empty PR or all-binary changes still need *something*.
if active_files == 0:
    rationale_parts.append("No active (non-removed) files in the PR.")
elif not candidates:
    candidates = ["code"]
    rationale_parts.append("Heuristic produced no candidates; defaulting to code.")

print(json.dumps({
    "trivial_docs_only": trivial_docs_only,
    "total_files": active_files,
    "buckets": buckets,
    "candidates": candidates,
    "rationale": " ".join(rationale_parts),
    "heuristic_status": "success",
}, ensure_ascii=False))
PY
