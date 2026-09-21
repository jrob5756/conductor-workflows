"""Evidence capture and fail-closed issue publication for the LogService audit."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse
import uuid


REPOSITORY = "azure-core-cto/log-service"
LABEL = "needs triage"
STAGES = ("baseline", "control", "repro", "removed-build", "removed-test", "diagnostic", "cleanup", "measurement")
CATEGORIES = {"bug": "B", "dead_code": "D", "stability": "S", "performance": "P"}

# Artifacts saved beside a candidate are inlined into its issue so a reader can
# reproduce it without access to this machine's run directory.
ARTIFACT_LANGUAGES = {
    ".cs": "csharp", ".ps1": "powershell", ".py": "python", ".sh": "bash",
    ".patch": "diff", ".diff": "diff", ".xml": "xml", ".json": "json",
    ".yaml": "yaml", ".yml": "yaml", ".proto": "protobuf", ".sql": "sql",
}
JOURNAL_FILES = frozenset((
    "result.json", "report.json", "resolution.json", "cleanup.json",
    "completed.json", "inflight.json", "cluster-used.json", "shortlist.json",
))
MAX_BODY = 60000
# Settling delay between the two inventory reads that clear a pending receipt.
RECONCILE_SETTLE = 15.0


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(argv, cwd=None, timeout=120):
    result = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"{argv[0]} failed ({result.returncode}): {result.stderr.strip()}\n"
            f"{result.stdout.strip()}"
        )
    return result.stdout.strip()


# Network faults that leave no server-side effect, so an idempotent read can be retried.
TRANSIENT = re.compile(
    r"wsarecv|connection (?:was )?(?:forcibly closed|reset|refused)|broken pipe|"
    r"EOF|timeout|timed out|temporary failure|502 Bad Gateway|503 Service Unavailable",
    re.IGNORECASE,
)


def gh_read(argv, attempts=4, pause=2.0):
    """Runs a read-only gh command, retrying transient network faults."""
    for attempt in range(1, attempts + 1):
        try:
            return run(argv)
        except RuntimeError as error:
            if attempt == attempts or not TRANSIENT.search(str(error)):
                raise
            time.sleep(pause * attempt)


def beneath(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"Artifact must be inside {root}: {relative}")
    return path


def inventory(state):
    pages = json.loads(gh_read([
        "gh", "api", "--hostname", state["host"], "--paginate", "--slurp",
        f"repos/{REPOSITORY}/issues?state=all&per_page=100",
    ]))
    entries = []
    for page in pages:
        if not isinstance(page, list):
            raise ValueError("Incomplete or invalid GitHub issue inventory.")
        for item in page:
            entries.append({
                "number": item["number"], "title": item["title"],
                "body": item["body"] or "", "state": item["state"],
                "url": item["html_url"], "is_pr": "pull_request" in item,
                "created_at": item.get("created_at") or "",
                "labels": sorted(label["name"] for label in item["labels"]),
            })
    return sorted(entries, key=lambda item: item["number"])


def read_state(run_dir):
    state = read_json(Path(run_dir) / "run.json")
    if Path(state["run_dir"]).resolve() != Path(run_dir).resolve():
        raise ValueError("Run directory does not match run.json.")
    return state


def load_state(run_dir):
    state = read_state(run_dir)
    owner = read_json(Path(state["lock"]) / "owner.json")
    if owner["run_dir"] != state["run_dir"]:
        raise ValueError("This run does not own the machine audit lock.")
    return state


def bootstrap(options):
    if os.name != "nt" or not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("Run from an elevated Windows terminal on the disposable local cluster.")
    if options.get("recover_run"):
        return recover(options["recover_run"])
    if options["environment"] not in ("redeploy", "reinstall", "existing"):
        raise ValueError("environment must be redeploy, reinstall, or existing.")
    cap = options["max_candidates"]
    if type(cap) not in (int, float) or not 1 <= cap <= 30 or int(cap) != cap:
        raise ValueError("max_candidates must be an integer from 1 through 30.")
    if type(options["publish"]) is not bool:
        raise ValueError("publish must be boolean.")
    for executable in ("git", "gh", "powershell.exe", "dotnet", "grpcurl"):
        if not shutil.which(executable):
            raise RuntimeError(f"Missing prerequisite: {executable}")
    root = Path(run(["git", "rev-parse", "--show-toplevel"])).resolve()
    remote = run(["git", "remote", "get-url", "origin"], root)
    match = re.fullmatch(r"git@([^:]+):(.+?)(?:\.git)?", remote)
    if match:
        host, repository = match.groups()
    else:
        uri = urlparse(remote)
        if uri.scheme != "https" or not uri.hostname or uri.username or uri.password:
            raise ValueError("origin must be an HTTPS or git@host GitHub remote without credentials.")
        host, repository = uri.hostname, uri.path.strip("/").removesuffix(".git")
    if repository.casefold() != REPOSITORY:
        raise ValueError(f"Run from {REPOSITORY}, not {repository}.")
    target = f"{host}/{REPOSITORY}"
    repo = json.loads(run([
        "gh", "repo", "view", target, "--json", "nameWithOwner,defaultBranchRef,url",
    ], root))
    if repo["nameWithOwner"].casefold() != REPOSITORY or urlparse(repo["url"]).hostname != host:
        raise ValueError("GitHub resolved a different repository or host.")
    branch = repo["defaultBranchRef"]["name"]
    run(["git", "fetch", "--no-tags", "origin", f"refs/heads/{branch}"], root)
    sha = run(["git", "rev-parse", "FETCH_HEAD"], root)
    home = Path(os.environ["LOCALAPPDATA"]) / "Conductor" / "log-service-audit"
    home.mkdir(parents=True, exist_ok=True)
    machine_home = Path(os.environ["PROGRAMDATA"]) / "Conductor" / "log-service-audit"
    machine_home.mkdir(parents=True, exist_ok=True)
    lock = machine_home / "cluster.lock"
    lock.mkdir()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    worktree = root.parent / (root.name + ".worktrees") / ("audit-" + run_id)
    state = {
        "run_dir": str(run_dir), "history_dir": str(run_dir.parent),
        "worktree": str(worktree), "source_checkout": str(root),
        "host": host, "target": target, "default_branch": branch, "sha": sha,
        "started": now(), "lock": str(lock), "options": options,
        "issues_file": str(run_dir / "issues.json"),
    }
    write_json(lock / "owner.json", {"run_dir": str(run_dir), "started": now()})
    write_json(run_dir / "run.json", state)
    # Preserve the lock after failure; an interrupted deployment must not overlap a new run.
    run(["git", "worktree", "add", "--detach", str(worktree), sha], root)
    write_json(run_dir / "issues.json", inventory(state))
    return {**state, "recovering": False, "summary": run_summary(state)}


def run_summary(state, **extra):
    """Compact run context for prompts.

    Prompts must not embed the whole script output: a script step's own stdout is
    merged into its output, so rendering that output whole ships the same JSON
    twice in every prompt, on every turn.
    """
    keys = ("run_dir", "worktree", "history_dir", "issues_file", "target", "sha", "default_branch")
    return {**{key: state[key] for key in keys}, "options": state["options"], **extra}


def worktree_status(state):
    return run(["git", "status", "--porcelain", "--untracked-files=all"], state["worktree"])


def assert_clean(state):
    """Require the worktree to hold nothing beyond preparation's own footprint.

    Deploying syncs checked-in reference DLLs to the installed runtime, so demanding
    a pristine worktree would reject the environment the audit just built. A cleaner
    worktree is fine — a candidate restoring more than it touched is not a problem.
    Only changes a candidate *added* matter.
    """
    if run(["git", "rev-parse", "HEAD"], state["worktree"]) != state["sha"]:
        raise ValueError("Audit worktree HEAD changed.")
    baseline_path = Path(state["run_dir"]) / "worktree-baseline.txt"
    baseline = baseline_path.read_text(encoding="utf-8") if baseline_path.is_file() else ""
    allowed = {line.strip() for line in baseline.splitlines() if line.strip()}
    current = {line.strip() for line in worktree_status(state).splitlines() if line.strip()}
    added = sorted(current - allowed)
    if added:
        raise ValueError(
            "Audit worktree has changes beyond the preparation baseline; restore only "
            f"audit-owned changes before continuing. Unexpected: {added[:10]}"
        )


def validate_plan(state, plan):
    sources = {}
    for category, prefix in CATEGORIES.items():
        discoveries = plan["discoveries"][category]["candidates"]
        if len(discoveries) > state["options"]["max_candidates"]:
            raise ValueError("Discovery category exceeded its candidate limit.")
        for item in discoveries:
            identifier = item["id"]
            if not re.fullmatch(prefix + r"[0-9]{3}", identifier) or identifier in sources:
                raise ValueError("Invalid or duplicate discovery ID.")
            if item["category"] != category:
                raise ValueError("Discovery category does not match its agent.")
            sources[identifier] = item
    candidates = index_by_id(plan["discovery"]["candidates"], "candidate")
    if len(candidates) > state["options"]["max_candidates"]:
        raise ValueError("Consolidated candidate limit exceeded.")
    seen = set()
    selected = set()
    for item in plan["discovery"]["mapping"]:
        if item["source_id"] not in sources or item["source_id"] in seen or not item["reason"].strip():
            raise ValueError("Every discovery needs exactly one explained consolidation disposition.")
        seen.add(item["source_id"])
        if item["disposition"] == "selected" and item["candidate_id"] in candidates:
            selected.add(item["candidate_id"])
        elif item["disposition"] != "deferred" or item["candidate_id"] != "":
            raise ValueError("Consolidation must select an existing candidate or explicitly defer.")
    if seen != sources.keys() or selected != candidates.keys():
        raise ValueError("Consolidation dropped a discovery or invented an unsupported candidate.")
    fingerprints = set()
    for candidate in candidates.values():
        if candidate["category"] not in CATEGORIES:
            raise ValueError("Invalid consolidated category.")
        key = fingerprint(candidate)
        if key in fingerprints:
            raise ValueError("Consolidation retained identical root-cause fingerprints.")
        fingerprints.add(key)


def queue(run_dir, plan):
    state = load_state(run_dir)
    validate_plan(state, plan)
    path = Path(run_dir) / "plan.json"
    if path.exists():
        if read_json(path) != plan:
            raise ValueError("Cannot replace a persisted discovery plan during recovery.")
    else:
        assert_clean(state)
        write_json(path, plan)
    summarize(run_dir)
    return {"candidates": [
        candidate for candidate in plan["discovery"]["candidates"]
        if not (Path(run_dir) / "candidates" / candidate["id"] / "completed.json").exists()
    ]}


def recover(run_dir):
    state = read_state(run_dir)
    if (Path(run_dir) / "completed.json").exists():
        return {**state, "recovering": True, "finalized": True, "final_result": finish(run_dir)}
    state = load_state(run_dir)
    if run(["git", "rev-parse", "HEAD"], state["worktree"]) != state["sha"]:
        raise ValueError("Cannot recover an audit whose worktree HEAD changed.")
    plan = read_json(Path(run_dir) / "plan.json")
    validate_plan(state, plan)
    return {**state, "recovering": True, "saved_plan": plan, "summary": run_summary(state)}


def candidate_context(run_dir, identifier):
    state = load_state(run_dir)
    plan = read_json(Path(run_dir) / "plan.json")
    candidates = index_by_id(plan["discovery"]["candidates"], "candidate")
    if identifier not in candidates:
        raise ValueError("Candidate is not in the persisted plan.")
    directory = Path(run_dir) / "candidates" / identifier
    return state, plan, candidates[identifier], directory


def require_active(run_dir, identifier):
    active = read_json(Path(run_dir) / "active.json")
    if active["id"] != identifier:
        raise ValueError("A different candidate owns the audit environment.")
    return candidate_context(run_dir, identifier)


def start_candidate(run_dir, identifier):
    state, plan, candidate, directory = candidate_context(run_dir, identifier)
    directory.mkdir(parents=True, exist_ok=True)
    completed = (directory / "completed.json").exists()
    for previous in plan["discovery"]["candidates"]:
        if previous["id"] == identifier:
            break
        if not (Path(run_dir) / "candidates" / previous["id"] / "completed.json").exists():
            raise ValueError("Previous candidate has not passed cleanup; refusing to start another.")
    active_path = Path(run_dir) / "active.json"
    if not completed:
        if active_path.exists():
            require_active(run_dir, identifier)
        else:
            assert_clean(state)
            write_json(active_path, {"id": identifier})
        if (directory / "inflight.json").exists():
            raise ValueError("A recorded command may still be running; inspect inflight.json before recovery.")
    result_path = directory / "result.json"
    return {
        **state, "candidate": candidate, "preparation": plan["preparation"],
        "completed": completed, "has_result": result_path.exists(),
        "result": read_json(result_path) if result_path.exists() else {},
        "resolved": (directory / "resolution.json").exists(),
        "candidate_dir": str(directory), "reports_file": str(Path(run_dir) / "report.json"),
        "summary": run_summary(state, candidate_dir=str(directory)),
    }


def handoff(run_dir, identifier, result):
    _, _, _, directory = require_active(run_dir, identifier)
    if result["id"] != identifier or result["status"] not in ("validated", "rejected", "blocked"):
        raise ValueError("Reproduction must report the active candidate and a valid disposition.")
    path = directory / "result.json"
    if path.exists() and read_json(path) != result:
        raise ValueError("Cannot replace saved reproduction evidence during recovery.")
    write_json(path, result)
    summarize(run_dir)
    return {"status": result["status"]}


CORE_MODULE_PATH = re.compile(r"[\\/]PowerShell[\\/](?:7[\\/])?Modules[\\/]?$", re.IGNORECASE)


def child_environment(argv):
    """Environment for a recorded child, or None to inherit unchanged.

    Windows PowerShell 5.1 shares PSModulePath with any PowerShell 7 parent, whose
    module directories come first and hold Core-only copies of modules such as
    Microsoft.PowerShell.Security. A 5.1 host cannot load those, so autoloading
    fails; dropping only the Core entries leaves Windows PowerShell and product
    module paths (for example the Service Fabric SDK) intact.
    """
    if os.name != "nt" or not argv or Path(argv[0]).name.casefold() not in ("powershell.exe", "powershell"):
        return None
    current = os.environ.get("PSModulePath", "")
    kept = [p for p in current.split(os.pathsep) if p.strip() and not CORE_MODULE_PATH.search(p.rstrip("\\/"))]
    if len(kept) == len([p for p in current.split(os.pathsep) if p.strip()]):
        return None
    return {**os.environ, "PSModulePath": os.pathsep.join(kept)}


def record(run_dir, name, stage, timeout, argv, scope="local"):
    state = load_state(run_dir)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", name):
        raise ValueError("Record name must be 1-80 letters, digits, hyphens, or underscores.")
    if not argv or timeout <= 0:
        raise ValueError("A command and positive timeout are required.")
    worktree = Path(state["worktree"])
    if run(["git", "rev-parse", "HEAD"], worktree) != state["sha"]:
        raise ValueError("Audit worktree HEAD moved away from the pinned SHA.")
    directory = Path(run_dir) / "records" / name
    directory.mkdir(parents=True, exist_ok=False)
    diff = run(["git", "diff", "--binary", "HEAD"], worktree)
    (directory / "before.patch").write_text(diff, encoding="utf-8")
    status = run(["git", "status", "--porcelain"], worktree)
    active_path = Path(run_dir) / "active.json"
    identifier = read_json(active_path)["id"] if active_path.exists() else None
    candidate_dir = None
    if identifier:
        _, plan, _, candidate_dir = require_active(run_dir, identifier)
        if scope == "cluster" and plan["preparation"]["environment_ready"] is not True:
            raise ValueError("Live candidate experiments require a ready, provenance-checked baseline.")
        if (candidate_dir / "inflight.json").exists():
            raise ValueError("Another recorded command is unfinished.")
        write_json(candidate_dir / "inflight.json", {"name": name, "argv": argv, "started": now()})
        if scope == "cluster":
            write_json(candidate_dir / "cluster-used.json", {"last_command": name})
    started = now()
    start = time.monotonic()
    timed_out = False
    with (directory / "stdout.txt").open("wb") as stdout, (directory / "stderr.txt").open("wb") as stderr:
        child = subprocess.Popen(
            argv, cwd=worktree, stdout=stdout, stderr=stderr, env=child_environment(argv),
        )
        if candidate_dir:
            write_json(candidate_dir / "inflight.json", {
                "name": name, "argv": argv, "pid": child.pid, "started": started,
            })
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                run(["taskkill.exe", "/PID", str(child.pid), "/T", "/F"])
            else:
                child.kill()
            code = child.wait(timeout=30)
    artifacts = {
        filename: file_digest(directory / filename)
        for filename in ("stdout.txt", "stderr.txt", "before.patch")
    }
    result = {
        "name": name, "stage": stage, "argv": argv, "cwd": str(worktree),
        "sha": state["sha"], "started": started, "ended": now(),
        "seconds": round(time.monotonic() - start, 3),
        "exit_code": code, "timed_out": timed_out, "status_before": status,
        "artifacts": artifacts, "candidate_id": identifier, "scope": scope,
    }
    write_json(directory / "record.json", result)
    if candidate_dir:
        (candidate_dir / "inflight.json").unlink()
    return {"record": str(Path("records") / name / "record.json"), **result}


def snapshot(run_dir):
    state = load_state(run_dir)
    entries = inventory(state)
    write_json(Path(run_dir) / "issues.json", entries)
    return {"path": state["issues_file"], "digest": digest(entries), "count": len(entries)}


def index_by_id(items, label):
    if not isinstance(items, list):
        raise ValueError(f"{label} must be an array.")
    result = {}
    for item in items:
        identifier = item["id"]
        if not re.fullmatch(r"C[0-9]{3}", identifier) or identifier in result:
            raise ValueError(f"Invalid or repeated {label} ID: {identifier}")
        result[identifier] = item
    return result


def fingerprint(candidate):
    values = [candidate[key].strip().casefold() for key in ("path", "symbol", "invariant")]
    values[0] = values[0].replace("\\", "/")
    return digest(values)


def resolve_artifact(state, candidate_id, reference):
    """Locate a saved artifact named either bare or run-directory-relative.

    Agents save artifacts beside their candidate and may cite them either way, so
    both spellings resolve rather than discarding a verified finding.
    """
    if not reference or not str(reference).strip():
        return None
    roots = [Path(state["run_dir"])]
    if candidate_id:
        roots.insert(0, Path(state["run_dir"]) / "candidates" / candidate_id)
    for root in roots:
        try:
            path = beneath(root, str(reference).strip())
        except (ValueError, OSError):
            continue
        if path.is_file():
            return path
    return None


def check_measurement(result):
    """Require the numbers behind a performance claim, not an adjective.

    A regression is only meaningful against a stated reference, with enough
    samples to separate it from noise, so the claim must carry both.
    """
    measurement = result.get("measurement")
    if not isinstance(measurement, dict):
        raise ValueError("A performance finding must report a measurement object.")
    for key in ("metric", "unit", "scenario", "threshold"):
        if not isinstance(measurement.get(key), str) or not measurement[key].strip():
            raise ValueError(f"Performance measurement is missing {key}.")
    numbers = {}
    for key in ("baseline_value", "observed_value", "samples"):
        value = measurement.get(key)
        if type(value) not in (int, float) or isinstance(value, bool):
            raise ValueError(f"Performance measurement needs a numeric {key}.")
        numbers[key] = value
    if numbers["samples"] < 2:
        raise ValueError("A performance finding needs at least two samples per side.")
    if numbers["baseline_value"] == numbers["observed_value"]:
        raise ValueError("Observed and reference values are identical; that is not a regression.")
    return measurement


def check_evidence(state, candidate, result):
    records = []
    paths = result["records"]
    if len({beneath(state["run_dir"], path) for path in paths}) != len(paths):
        raise ValueError("The same evidence record cannot count as two executions.")
    for filename in paths:
        path = beneath(state["run_dir"], filename)
        entry = read_json(path)
        if state.get("candidate_id") and entry.get("candidate_id") != state["candidate_id"]:
            raise ValueError(f"Evidence belongs to a different candidate: {filename}")
        if entry["sha"] != state["sha"] or entry["cwd"] != state["worktree"] or type(entry["exit_code"]) is not int:
            raise ValueError(f"Invalid or stale evidence: {filename}")
        if not entry["argv"] or not entry["started"] or not entry["ended"]:
            raise ValueError(f"Incomplete execution record: {filename}")
        for artifact in ("stdout.txt", "stderr.txt", "before.patch"):
            if file_digest(path.parent / artifact) != entry["artifacts"][artifact]:
                raise ValueError(f"Evidence changed after recording: {filename}: {artifact}")
        entry["diff"] = (path.parent / "before.patch").read_text(encoding="utf-8")
        records.append(entry)
    # A killed, timed-out process can still exit nonzero/zero by accident, so a
    # timeout can never itself satisfy a passing stage or count as a genuine
    # assertion failure. Superseded timed-out attempts may still be listed for
    # transparency; they are simply inert for every requirement below.
    passed = lambda stage: any(
        r["stage"] == stage and r["exit_code"] == 0 and not r["timed_out"] for r in records
    )
    if candidate["category"] == "dead_code":
        if not passed("baseline") or not result["reachability"].strip() or not result["patch"]:
            raise ValueError("Dead code requires baseline, reachability analysis, and a deletion patch.")
        for stage in ("removed-build", "removed-test"):
            if not any(
                r["stage"] == stage and r["exit_code"] == 0 and not r["timed_out"] and r["diff"]
                for r in records
            ):
                raise ValueError(f"Dead code requires a passing {stage} with its deletion diff.")
    elif candidate["category"] == "performance":
        # A measurement run succeeds even when it records a regression, so the
        # failing-assertion rule cannot apply. Require a reference measurement, a
        # repeated measurement of the suspect path, and the numbers themselves.
        if not passed("baseline"):
            raise ValueError("A performance finding requires a recorded reference measurement.")
        if sum(
            r["stage"] == "measurement" and r["exit_code"] == 0 and not r["timed_out"]
            for r in records
        ) < 2:
            raise ValueError("A performance finding requires at least two successful measurement runs.")
        check_measurement(result)
    elif not passed("control") or sum(
        r["stage"] == "repro" and r["exit_code"] != 0 and not r["timed_out"] for r in records
    ) < 2:
        raise ValueError("Bug/stability proof requires a passing control and two failing reproductions.")
    patch = resolve_artifact(state, candidate["id"], result["patch"])
    if patch is not None and not patch.read_text(encoding="utf-8").strip():
        raise ValueError(f"Reproduction patch is empty: {result['patch']}")
    # A deletion patch is core proof for dead code. For bug/stability the proof is
    # the control plus failing reproductions, and the issue inlines whatever the
    # candidate directory holds, so an unresolvable reference must not discard a
    # verified finding at the last step.
    if candidate["category"] == "dead_code" and patch is None:
        raise ValueError(f"Dead code requires a resolvable deletion patch: {result['patch']!r}")
    for key in ("reproduction", "expected", "actual", "impact", "explanation"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ValueError(f"Validated finding is missing {key}.")
    return records


def validate_report(state, report, entries):
    candidates = index_by_id(report["discovery"]["candidates"], "candidate")
    results = index_by_id(report["reproduction"]["results"], "reproduction")
    decisions = index_by_id(report["verification"]["decisions"], "decision")
    if len(candidates) > state["options"]["max_candidates"]:
        raise ValueError("Candidate limit exceeded.")
    if candidates.keys() != results.keys() or candidates.keys() != decisions.keys():
        raise ValueError("Every candidate must have exactly one reproduction and decision.")
    if report["snapshot_digest"] != digest(entries):
        raise ValueError("The reviewed issue snapshot changed.")
    numbers = {item["number"] for item in entries}
    approved = []
    seen = set()
    for identifier, candidate in candidates.items():
        result, decision = results[identifier], decisions[identifier]
        if candidate["category"] not in CATEGORIES:
            raise ValueError(f"Invalid category: {identifier}")
        if result["status"] not in ("validated", "rejected", "blocked"):
            raise ValueError(f"Invalid reproduction status: {identifier}")
        if decision["verdict"] not in ("approved", "rejected", "blocked", "duplicate"):
            raise ValueError(f"Invalid verification verdict: {identifier}")
        if not decision["reason"].strip():
            raise ValueError(f"Missing independent decision rationale: {identifier}")
        if decision["verdict"] == "duplicate":
            other = decision.get("duplicate_candidate_id", "")
            if other:
                other_path = Path(state["run_dir"]) / "candidates" / other / "resolution.json"
                if (not re.fullmatch(r"C[0-9]{3}", other) or other == identifier
                        or decision["duplicate_number"] != 0 or not other_path.is_file()):
                    raise ValueError("Duplicate does not name an earlier resolved candidate.")
                other_report = read_json(other_path.parent / "report.json")
                if not any(d["verdict"] == "approved" for d in other_report["verification"]["decisions"]):
                    raise ValueError("An unapproved candidate cannot suppress a new finding.")
            elif decision["duplicate_number"] not in numbers:
                raise ValueError(f"Duplicate does not name an inventory issue/PR: {identifier}")
        elif decision["duplicate_number"] != 0 or decision.get("duplicate_candidate_id", ""):
            raise ValueError(f"Unexpected duplicate number: {identifier}")
        if decision["verdict"] != "approved":
            continue
        if (result["status"] != "validated" or decision["evidence_checked"] is not True
                or decision["secrets_checked"] is not True):
            raise ValueError(f"Finding has not passed both verification gates: {identifier}")
        for key in ("path", "symbol", "invariant", "title"):
            if not isinstance(candidate[key], str) or not candidate[key].strip():
                raise ValueError(f"Missing candidate {key}: {identifier}")
        source = beneath(state["worktree"], candidate["path"])
        if not source.is_file():
            relative = source.relative_to(Path(state["worktree"]).resolve()).as_posix()
            if run(["git", "cat-file", "-t", f"{state['sha']}:{relative}"], state["worktree"]) != "blob":
                raise ValueError(f"Candidate source file does not exist in the pinned commit: {identifier}")
        key = fingerprint(candidate)
        if key in seen:
            raise ValueError("Duplicate root-cause fingerprint inside this run.")
        seen.add(key)
        evidence = check_evidence(state, candidate, result)
        approved.append((candidate, result, decision, key, evidence))
    return approved


def fence_indented_code(text):
    """Fence indented code blocks so Markdown renders them as code.

    Agents often indent sources by two spaces, which Markdown renders as running
    prose rather than a code block.
    """
    lines = str(text).split("\n")
    output, block = [], []

    def flush():
        if not block:
            return
        code = [line for line in block if line.strip()]
        if len(code) >= 2 and any(re.search(r"[{};]|=>|\(\)|\breturn\b", line) for line in code):
            indent = min(len(line) - len(line.lstrip()) for line in code)
            body = "\n".join(line[indent:] if line.strip() else "" for line in block).strip("\n")
            output.append(fenced("", body))
        else:
            output.extend(block)
        block.clear()

    for line in lines:
        if re.match(r"^(\t| {2,})\S", line) or (block and not line.strip()):
            block.append(line)
            continue
        flush()
        output.append(line)
    flush()
    return "\n".join(output)


def sanitize_paths(text, state, candidate_id, inlined=()):
    """Rewrite machine-local references so public text stands on its own.

    Absolute worktree/run-directory paths, run-relative artifact references and
    recorder invocations mean nothing to a reader who only has the repository,
    so they become repository-relative paths, inlined-artifact names or the
    underlying child command. Only artifacts actually carried by the issue are
    annotated as inlined.
    """
    text = str(text)
    for base in (state["run_dir"], state["worktree"]):
        for variant in (base + "\\", base + "/", base):
            text = re.sub(re.escape(variant), "", text, flags=re.IGNORECASE)
    name = r"[\w\-]+(?:\.[\w\-]+)+"
    text = re.sub(r"records[\\/](" + name + r"|[\w\-]+)[\\/]record\.json", r"`\1`", text)
    text = re.sub(
        r"candidates[\\/]" + re.escape(candidate_id) + r"[\\/](" + name + r")",
        lambda m: f"`{m.group(1)}`" + (" (inlined below)" if m.group(1) in inlined else ""),
        text,
    )
    text = re.sub(r"records[\\/]([\w\-]+)", r"`\1`", text)
    # Audit-internal locations and recorder jargon mean nothing to a reader.
    text = re.sub(r"recorded with\s+--scope\s+local\b", "recorded locally", text, flags=re.IGNORECASE)
    text = re.sub(
        r"recorded with\s+--scope\s+cluster\b", "recorded against the live cluster",
        text, flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*--scope\s+(?:local|cluster)\b", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\s*(?:retained|saved|stored|kept)?\s*(?:under|in|at)\s+the\s+run\s+director(?:y|ies)"
        r"(?:\s+(?:at|in|under)\s+\S+)?",
        " in this audit's local evidence bundle", text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"candidates[\\/][\w\-]+[\\/][\w.\-\\/]*", "this audit's local evidence bundle", text,
    )
    text = re.sub(r"\brun director(y|ies)\b", "audit evidence bundle", text, flags=re.IGNORECASE)
    # Leave only the command being measured, not this audit's recorder wrapper.
    text = re.sub(r'python\s+"?[^"\s]*audit\.py"?\s+record\b[^\n]*?--\s+', "", text)
    text = re.sub(r"<run-?dir>|<worktree>|<workflows>", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\s*\((?:relative to|saved (?:in|under)|paths? relative to)[^)]*run[- ]?dir(?:ectory)?\)",
        "", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"[ \t]+([,.;:])", r"\1", text)
    return text.strip()


def sanitize_prose(text, state, candidate_id, inlined=()):
    """Sanitize paths and fence indented code; for prose only, never for artifacts."""
    return fence_indented_code(sanitize_paths(text, state, candidate_id, inlined))


def candidate_artifacts(directory):
    """Reproduction sources saved for a candidate, preferring files over patches."""
    if not Path(directory).is_dir():
        return []
    sources, patches = [], []
    for path in sorted(Path(directory).iterdir()):
        suffix = path.suffix.lower()
        if not path.is_file() or path.name in JOURNAL_FILES or suffix not in ARTIFACT_LANGUAGES:
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not content:
            continue
        entry = (path.name, ARTIFACT_LANGUAGES[suffix], content)
        (patches if suffix in (".patch", ".diff") else sources).append(entry)
    return sources or patches


def command_of(entry, state):
    parts = []
    for item in entry["argv"]:
        text = sanitize_paths(item, state, entry.get("candidate_id") or "").strip("`")
        parts.append(f'"{text}"' if " " in text else text)
    return " ".join(parts)


def fenced(language, content):
    fence = "```"
    while fence in content:
        fence += "`"
    return f"{fence}{language}\n{content}\n{fence}"


def collapsed(summary, content):
    return f"<details>\n<summary>{summary}</summary>\n\n{content}\n\n</details>"


def issue_body(state, candidate, result, decision, key, evidence, environment_ready=None):
    identifier = candidate["id"]
    artifacts = candidate_artifacts(Path(state["run_dir"]) / "candidates" / identifier)
    inlined = {name for name, _, _ in artifacts}
    clean = lambda value: sanitize_prose(value, state, identifier, inlined)
    rows = "\n".join(
        f"| `{item['name']}` | {item['stage']} | `{item['exit_code']}` | {item['seconds']}s |"
        for item in evidence
    )
    commands = "\n\n".join(
        f"# {item['name']} ({item['stage']}, exit {item['exit_code']})\n{command_of(item, state)}"
        for item in evidence if not item.get("timed_out")
    )
    if environment_ready is False:
        validated_on = "in-process tests only; no live Service Fabric cluster was available this run"
    elif environment_ready is True:
        validated_on = f"disposable local Service Fabric cluster (`{state['options']['environment']}` mode)"
    else:
        validated_on = f"`{state['options']['environment']}` mode"

    sections = [
        f"<!-- log-service-audit:{key} -->",
        "| | |\n|---|---|\n"
        f"| **Category** | {candidate['category']} |\n"
        f"| **Source** | `{candidate['path']}` |\n"
        f"| **Symbol** | `{candidate['symbol']}` |\n"
        f"| **Broken invariant** | {clean(candidate['invariant'])} |\n"
        f"| **Commit** | `{state['sha']}` (`{state['default_branch']}`) |\n"
        f"| **Validated on** | {validated_on} |",
        "## Summary\n\n" + clean(result["explanation"]),
        "## Impact\n\n" + clean(result["impact"]),
        "## Expected vs actual\n\n"
        "**Expected:** " + clean(result["expected"]) + "\n\n"
        "**Actual:** " + clean(result["actual"]),
    ]

    measurement = result.get("measurement")
    if isinstance(measurement, dict) and measurement.get("metric"):
        delta = ""
        try:
            base, seen = float(measurement["baseline_value"]), float(measurement["observed_value"])
            if base:
                delta = f"\n| **Change** | {(seen - base) / abs(base) * 100:+.1f}% |"
        except (TypeError, ValueError, KeyError):
            delta = ""
        sections.append(
            "## Measurement\n\n| | |\n|---|---|\n"
            f"| **Metric** | {clean(measurement['metric'])} ({clean(measurement['unit'])}) |\n"
            f"| **Scenario** | {clean(measurement['scenario'])} |\n"
            f"| **Reference** | {measurement['baseline_value']} |\n"
            f"| **Observed** | {measurement['observed_value']} |{delta}\n"
            f"| **Samples per side** | {measurement['samples']} |\n"
            f"| **Threshold** | {clean(measurement['threshold'])} |"
        )

    sections.append("## Reproduction\n\n" + clean(result["reproduction"]))

    artifacts = list(artifacts)
    for name, language, content in artifacts:
        sections.append(collapsed(
            f"<code>{name}</code> — full source, add this to the repository as-is",
            fenced(language, sanitize_paths(content, state, identifier)),
        ))
    if commands:
        sections.append("### Commands\n\nRun from the repository root at the commit above.\n\n"
                        + fenced("powershell", commands))
    sections.extend([
        "## Execution evidence\n\n| Record | Stage | Exit code | Duration |\n|---|---|---|---|\n" + rows,
        collapsed("Reachability and compatibility analysis", clean(result["reachability"])),
        collapsed("Independent verification and duplicate review", clean(decision["reason"])),
        "---\n\nFiled by an automated audit. Raw logs stay on the audit machine and are not "
        "uploaded. Awaiting human triage; no fix is included.",
    ])

    body = "\n\n".join(sections) + "\n"
    while len(body) > MAX_BODY and artifacts:
        artifacts.pop()
        dropped = len(candidate_artifacts(Path(state["run_dir"]) / "candidates" / identifier)) - len(artifacts)
        trimmed = [s for s in sections if not s.startswith("<details>\n<summary><code>")]
        rebuilt = trimmed[:6] + [
            collapsed(f"<code>{name}</code> — full source, add this to the repository as-is",
                      fenced(language, sanitize_paths(content, state, identifier)))
            for name, language, content in artifacts
        ] + trimmed[6:]
        body = "\n\n".join(rebuilt) + (
            f"\n\n> {dropped} reproduction artifact(s) omitted to fit GitHub's size limit.\n"
        )
    if re.search(
        r"-----BEGIN .*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|Authorization\s*:\s*Bearer\s+\S+",
        candidate["title"] + "\n" + body, re.IGNORECASE,
    ):
        raise ValueError("Potential credential found in public issue text.")
    if len(body) > MAX_BODY:
        raise ValueError("Issue body is too large; provide a concise self-contained reproduction.")
    return body


def ensure_label(state):
    pages = json.loads(gh_read([
        "gh", "api", "--hostname", state["host"], "--paginate", "--slurp",
        f"repos/{REPOSITORY}/labels?per_page=100",
    ]))
    if not any(item["name"] == LABEL for page in pages for item in page):
        run([
            "gh", "label", "create", LABEL, "-R", state["target"],
            "--color", "FBCA04", "--description", "Validated finding awaiting human triage",
        ])


def publish(run_dir, report, candidate_id=None):
    state = load_state(run_dir)
    directory = Path(run_dir) if candidate_id is None else Path(run_dir) / "candidates" / candidate_id
    if candidate_id:
        state = {**state, "candidate_id": candidate_id}
    write_json(directory / "report.json", report)
    entries = read_json(Path(run_dir) / "issues.json")
    approved = validate_report(state, report, entries)
    receipts = Path(run_dir) / "receipts"
    receipts.mkdir(exist_ok=True)
    recovered = {}
    for candidate in report["discovery"]["candidates"]:
        key = fingerprint(candidate)
        receipt = receipts / (key + ".json")
        if not receipt.exists():
            continue
        marker = f"<!-- log-service-audit:{key} -->"
        matches = [item for item in inventory(state) if marker in item["body"]]
        if not matches:
            raise RuntimeError(
                f"Uncertain previous publication for {candidate['id']}. No retry is safe; inspect {receipt}."
            )
        match = matches[0]
        if LABEL not in match["labels"]:
            raise RuntimeError("Previously submitted issue is missing needs triage; inspect it manually.")
        write_json(receipt, {"status": "published", "number": match["number"], "url": match["url"]})
        recovered[candidate["id"]] = {"id": candidate["id"], "status": "recovered", "url": match["url"]}
    drafts = directory / "drafts"
    drafts.mkdir(exist_ok=True)
    prepared = []
    for candidate, result, decision, key, evidence in approved:
        if candidate["id"] in recovered:
            continue
        body = issue_body(
            state, candidate, result, decision, key, evidence,
            report.get("preparation", {}).get("environment_ready"),
        )
        body_file = drafts / (key + ".md")
        body_file.write_text(body, encoding="utf-8")
        prepared.append((candidate, key, body_file))
    outcomes = list(recovered.values())
    for candidate, key, body_file in prepared:
        if candidate_id:
            prior = previous_finding(run_dir, candidate_id, key)
            if prior:
                outcomes.append({"id": candidate_id, "status": "duplicate", "candidate_id": prior})
                continue
        marker = f"<!-- log-service-audit:{key} -->"
        fresh = inventory(state)
        matches = [item for item in fresh if marker in item["body"]]
        receipt = receipts / (key + ".json")
        if matches:
            match = matches[0]
            if receipt.exists() and LABEL not in match["labels"]:
                raise RuntimeError("Previously submitted issue is missing needs triage; inspect it manually.")
            outcomes.append({"id": candidate["id"], "status": "duplicate", "url": match["url"]})
            if receipt.exists():
                write_json(receipt, {"status": "published", "number": match["number"], "url": match["url"]})
            continue
        if receipt.exists():
            raise RuntimeError(
                f"Uncertain previous publication for {candidate['id']}. No retry is safe; inspect {receipt}."
            )
        submitted = {
            read_json(path)["number"]
            for path in receipts.glob("*.json") if read_json(path).get("status") == "published"
        }
        reviewed = {item["number"] for item in entries}
        appeared = [
            item["number"] for item in fresh
            if not item["is_pr"] and item["number"] not in reviewed and item["number"] not in submitted
        ]
        if appeared:
            raise RuntimeError(
                "Issues filed after duplicate review ("
                + ", ".join(f"#{number}" for number in sorted(appeared))
                + "); re-run duplicate verification before publishing."
            )
        if not state["options"]["publish"]:
            outcomes.append({"id": candidate["id"], "status": "draft", "path": str(body_file)})
            continue
        ensure_label(state)
        with receipt.open("x", encoding="utf-8") as stream:
            json.dump({"status": "pending", "marker": marker, "started": now()}, stream)
        url = run([
            "gh", "issue", "create", "-R", state["target"],
            "--title", candidate["title"], "--body-file", str(body_file), "--label", LABEL,
        ])
        created = json.loads(run([
            "gh", "issue", "view", url, "-R", state["target"], "--json", "number,url,body,labels",
        ]))
        if marker not in created["body"] or LABEL not in [label["name"] for label in created["labels"]]:
            raise RuntimeError("Created issue failed marker/label verification; inspect pending receipt.")
        write_json(receipt, {"status": "published", "number": created["number"], "url": created["url"]})
        outcomes.append({"id": candidate["id"], "status": "created", "url": created["url"]})
    report["publication"] = outcomes
    write_json(directory / "report.json", report)
    return {
        "outcomes": outcomes, "report": str(directory / "report.json"),
        "coverage": report["discovery"]["coverage"],
        "environment_ready": report["preparation"]["environment_ready"],
        "blockers": report["preparation"]["blockers"],
    }


def previous_finding(run_dir, identifier, key):
    for path in (Path(run_dir) / "candidates").glob("*/resolution.json"):
        if path.parent.name == identifier:
            continue
        report = read_json(path.parent / "report.json")
        if (any(d["verdict"] == "approved" for d in report["verification"]["decisions"])
                and any(fingerprint(c) == key for c in report["discovery"]["candidates"])):
            return path.parent.name
    return None


def block_candidate(run_dir, identifier, reason):
    """Record a candidate as blocked so the run can continue with the next one.

    A stage failure that leaves the environment intact — a refused publication
    gate, a failed inventory refresh — should cost one candidate, not the whole
    audit. Cleanup still runs afterwards, and the block is reported.
    """
    _, _, _, directory = require_active(run_dir, identifier)
    path = directory / "resolution.json"
    if not path.exists():
        write_json(path, {
            "outcomes": [], "verdict": "blocked",
            "reason": str(reason).strip() or "Stage failed before publication.",
            "blocked_at": now(),
        })
    summarize(run_dir)
    return read_json(path)


POWERSHELL = ("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass")

# Baseline suites from AGENTS.md, with the documented default filters/flags.
BASELINE_SUITES = (
    ("wal-integration", r"tests\integration\LogService.Wal.IntegrationTests\LogService.Wal.IntegrationTests.csproj",
     ["--filter", "Category!=MtlsEnforce"]),
    ("plugin-integration", r"tests\integration\LogService.Plugin.IntegrationTests\LogService.Plugin.IntegrationTests.csproj",
     ["--filter", "Category!=RequiresServer", "-p:TestHooksEnabled=true", "-p:FileTracerEnabled=true"]),
    ("replicator-plugin-unit", r"tests\unit\LogService.ReplicatorPlugin.Tests\LogService.ReplicatorPlugin.Tests.csproj",
     ["-p:TestHooksEnabled=true", "-p:FileTracerEnabled=true"]),
    ("security-unit", r"tests\unit\LogService.Security.Tests\LogService.Security.Tests.csproj", []),
)


def powershell_file(script, *arguments):
    return [*POWERSHELL, "-File", script, *arguments]


def prepare(run_dir):
    """Build the audit baseline deterministically.

    Deployment and baseline testing are fixed script invocations, so running them
    here makes the baseline reproducible across runs and keeps `environment_ready`
    a fact derived from observed exit codes rather than a model's judgement.
    """
    state = load_state(run_dir)
    mode = state["options"]["environment"]
    worktree = Path(state["worktree"])
    steps, blockers, warnings = [], [], []

    def run_step(name, stage, timeout, argv, scope, required=True):
        entry = {"name": name, "required": required, "scope": scope}
        try:
            result = record(run_dir, name, stage, timeout, argv, scope)
            entry.update(exit_code=result["exit_code"], timed_out=result["timed_out"],
                         seconds=result["seconds"], record=result["record"])
            if result["exit_code"] != 0 or result["timed_out"]:
                reason = "timed out" if result["timed_out"] else f"exit {result['exit_code']}"
                (blockers if required else warnings).append(f"{name}: {reason}")
                entry["ok"] = False
            else:
                entry["ok"] = True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            entry.update(ok=False, error=str(exc)[:300])
            (blockers if required else warnings).append(f"{name}: {exc}")
        steps.append(entry)
        return entry["ok"]

    if mode == "reinstall":
        run_step("install-publicbuild", "baseline", 5400,
                 powershell_file(r".\scripts\cluster\Install-PublicBuild.ps1"), "cluster")
    elif mode == "redeploy":
        run_step("deploy-replicator-plugin", "baseline", 1800, [
            *POWERSHELL, "-Command",
            ". .\\scripts\\cluster\\lib\\install-helpers.ps1; Deploy-ReplicatorPlugin -BuildConfiguration Debug",
        ], "cluster")

    deployed = not blockers
    if deployed:
        # -File flattens an array argument into one string, which the script's own
        # sliceN validation then rejects, so pass the array through -Command.
        deployed = run_step("deploy-logservice", "baseline", 3600, [
            *POWERSHELL, "-Command",
            "& .\\scripts\\log-service\\Deploy-LogService.ps1 "
            "-Slices @('slice1','slice2','slice3') -WaitForHealthy",
        ], "cluster")
    if deployed:
        deployed = run_step("deploy-sampleapp", "baseline", 2400, powershell_file(
            r".\scripts\samples\Deploy-SampleApp.ps1", "-WaitForHealthy",
        ), "cluster")

    for name, project, extra in BASELINE_SUITES:
        if (worktree / project).is_file():
            run_step(f"baseline-{name}", "baseline", 3600,
                     ["dotnet", "test", project, *extra], "local")
        else:
            steps.append({"name": f"baseline-{name}", "ok": False, "required": False,
                          "error": f"project not found: {project}"})

    # Readiness means live experiments can run, so prove it against the cluster
    # itself. Smoke is a broader end-to-end signal: its failure is reported, but it
    # does not by itself veto experiments on a demonstrably healthy cluster.
    if deployed:
        deployed = run_step("cluster-health", "baseline", 600, [
            *POWERSHELL, "-Command",
            ". .\\scripts\\util\\ensure-sf-connection.ps1 -ConnectionEndpoint 'localhost:19000' | Out-Null; "
            "$h = (Get-ServiceFabricClusterHealth).AggregatedHealthState; "
            "if ($h -ne 'Ok') { throw \"Cluster health is $h\" }; "
            "$bad = @(Get-ServiceFabricApplication | Where-Object { "
            "(Get-ServiceFabricApplicationHealth -ApplicationName $_.ApplicationName)"
            ".AggregatedHealthState -ne 'Ok' }); "
            "if ($bad.Count) { throw \"Unhealthy applications: $($bad.Count)\" }; "
            "Write-Output 'cluster and applications healthy'",
        ], "cluster")

    if deployed:
        run_step("smoke", "baseline", 5400, powershell_file(
            r".\scripts\tests\smoke\Run-SmokeTest.ps1", "-SkipWfBuild", "-SkipLogServiceDeploy",
        ), "cluster", required=False)

    provenance = collect_provenance(state)
    environment_ready = deployed and not blockers
    # Deploying legitimately touches tracked reference DLLs; record that footprint so
    # later stages can tell preparation's changes from a candidate's leftovers.
    baseline = worktree_status(state)
    (Path(run_dir) / "worktree-baseline.txt").write_text(baseline, encoding="utf-8")
    result = {
        "ok": True, "environment_ready": bool(environment_ready), "mode": mode,
        "steps": steps, "blockers": blockers, "warnings": warnings, "provenance": provenance,
        "worktree_baseline": [line for line in baseline.splitlines() if line.strip()],
        "baseline_records": [s["record"] for s in steps if s.get("record")],
        "summary": (
            f"{mode}: {sum(1 for s in steps if s.get('ok'))}/{len(steps)} steps succeeded; "
            + ("environment ready for live experiments" if environment_ready
               else "live experiments blocked, local-only evidence remains valid")
            + (f"; non-blocking failures: {'; '.join(warnings)}" if warnings else "")
        ),
    }
    write_json(Path(run_dir) / "preparation.json", result)
    return result


def collect_provenance(state):
    """Record what was actually deployed, so live evidence can be tied to this source."""
    worktree = state["worktree"]
    facts = {"sha": state["sha"]}
    probes = {
        "dotnet": ["dotnet", "--version"],
        "cluster_health": [
            *POWERSHELL, "-Command",
            ". .\\scripts\\util\\ensure-sf-connection.ps1 -ConnectionEndpoint 'localhost:19000'; "
            "(Get-ServiceFabricClusterHealth).AggregatedHealthState",
        ],
        "applications": [
            *POWERSHELL, "-Command",
            ". .\\scripts\\util\\ensure-sf-connection.ps1 -ConnectionEndpoint 'localhost:19000'; "
            "(Get-ServiceFabricApplication | ForEach-Object { $_.ApplicationName.ToString() }) -join ','",
        ],
    }
    for name, argv in probes.items():
        try:
            facts[name] = run(argv, worktree, timeout=300)[:400]
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            facts[name] = f"unavailable: {str(exc)[:200]}"
    plugin = Path(r"C:\Program Files\Microsoft Service Fabric\bin\Fabric\Fabric.Code"
                  r"\NS_11\ReplicatorPlugins\Microsoft.ServiceFabric.LogService.ReplicatorPlugin")
    facts["installed_plugin_dlls"] = (
        {p.name: file_digest(p)[:16] for p in sorted(plugin.glob("*.dll"))}
        if plugin.is_dir() else "not installed"
    )
    return facts


def preflight(run_dir):
    """Assert prerequisites deterministically before any agent time is spent.

    A broken toolchain otherwise surfaces as an agent improvising for half an
    hour and guessing at the cause, so each check reports a concrete blocker.
    """
    state = load_state(run_dir)
    mode = state["options"]["environment"]
    checks, blockers = [], []

    def note(name, ok, detail, fatal=False):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok and fatal:
            blockers.append(f"{name}: {detail}")

    for tool in ("git", "gh", "dotnet", "powershell.exe", "grpcurl"):
        found = shutil.which(tool)
        note(f"tool:{tool}", found, found or "not on PATH", fatal=True)

    needs_powershell = mode in ("redeploy", "reinstall")
    probe = Path(run_dir) / "preflight-signature.ps1"
    probe.write_text(
        "$s = Get-AuthenticodeSignature -LiteralPath "
        "(Join-Path $env:SystemRoot 'System32\\notepad.exe')\n"
        "Write-Output $s.Status\n",
        encoding="utf-8",
    )
    argv = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File", str(probe),
    ]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=180, env=child_environment(argv),
        )
        signed = completed.returncode == 0 and "Valid" in completed.stdout
        detail = (completed.stdout + completed.stderr).strip()[:300] or "no output"
    except (OSError, subprocess.SubprocessError) as exc:
        signed, detail = False, str(exc)[:300]
    note("powershell:authenticode", signed, detail, fatal=needs_powershell)

    fabric = shutil.which("FabricHostSvc") or Path(
        r"C:\Program Files\Microsoft Service Fabric\bin\Fabric\Fabric.Code"
    ).is_dir()
    note("servicefabric:runtime", bool(fabric),
         "present" if fabric else "not installed (reinstall mode will install it)",
         fatal=(mode == "existing"))

    sdk = Path(r"C:\Program Files\Microsoft SDKs\Service Fabric").is_dir()
    note("servicefabric:sdk", sdk, "present" if sdk else "not installed", fatal=(mode == "existing"))

    result = {"ok": not blockers, "mode": mode, "checks": checks, "blockers": blockers}
    write_json(Path(run_dir) / "preflight.json", result)
    return result


def tokens_of(*values):
    return {
        token for value in values
        for token in re.split(r"[^A-Za-z0-9]+", str(value).casefold())
        if len(token) > 3
    }


def shortlist(run_dir, identifier, limit=25):
    """Rank existing issues/PRs by overlap with a candidate's root cause.

    Reviewing the whole inventory inside the model costs far more than ranking it
    here; the agent still gets the full file path to widen the search.
    """
    state = load_state(run_dir)
    plan = read_json(Path(run_dir) / "plan.json")
    candidate = index_by_id(plan["discovery"]["candidates"], "candidate")[identifier]
    needle = tokens_of(
        candidate["symbol"], candidate["path"], candidate["invariant"], candidate["title"],
    )
    ranked = []
    for item in read_json(Path(run_dir) / "issues.json"):
        overlap = needle & tokens_of(item["title"], item["body"][:4000])
        if overlap:
            ranked.append({
                "number": item["number"], "state": item["state"], "is_pr": item["is_pr"],
                "title": item["title"], "url": item["url"],
                "score": len(overlap), "shared_terms": sorted(overlap)[:12],
            })
    ranked.sort(key=lambda entry: (-entry["score"], entry["number"]))
    result = {
        "candidate_id": identifier, "inventory_file": state["issues_file"],
        "inventory_count": len(read_json(Path(run_dir) / "issues.json")),
        "ranked_count": len(ranked), "shortlist": ranked[:limit],
    }
    directory = Path(run_dir) / "candidates" / identifier
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "shortlist.json", result)
    return result


def resolve_candidate(run_dir, identifier, verification=None):
    _, plan, candidate, directory = require_active(run_dir, identifier)
    if (directory / "resolution.json").exists():
        return read_json(directory / "resolution.json")
    result = read_json(directory / "result.json")
    report = {
        "preparation": plan["preparation"],
        "discovery": {"coverage": plan["discovery"]["coverage"], "candidates": [candidate]},
        "reproduction": {"results": [result]},
    }
    if result["status"] != "validated":
        decision = {
            "id": identifier, "verdict": result["status"],
            "reason": "Reproduction did not validate this candidate: " + result["explanation"],
            "duplicate_number": 0, "duplicate_candidate_id": "",
            "evidence_checked": False, "secrets_checked": False,
        }
        report["verification"] = {"decisions": [decision]}
        report["publication"] = []
        write_json(directory / "report.json", report)
        outcome = {"outcomes": [], "verdict": result["status"]}
    else:
        if verification is None:
            raise ValueError("A validated reproduction requires an independent verification decision.")
        report["verification"] = {"decisions": [verification["decision"]]}
        report["snapshot_digest"] = verification["snapshot_digest"]
        outcome = publish(run_dir, report, identifier)
    write_json(directory / "resolution.json", outcome)
    summarize(run_dir)
    return outcome


def complete_candidate(run_dir, identifier, cleanup):
    state, _, _, directory = require_active(run_dir, identifier)
    write_json(directory / "cleanup.json", cleanup)
    if cleanup["ready"] is not True or not cleanup["reason"].strip():
        raise ValueError("Candidate cleanup did not establish a safe environment for the next candidate.")
    if not (directory / "resolution.json").exists() or (directory / "inflight.json").exists():
        raise ValueError("Candidate resolution or command execution is unfinished.")
    assert_clean(state)
    if (directory / "cluster-used.json").exists():
        command = (
            "$ErrorActionPreference='Stop'; "
            ". .\\scripts\\util\\ensure-sf-connection.ps1 -ConnectionEndpoint 'localhost:19000'; "
            "$Health=Get-ServiceFabricClusterHealth; "
            "if ($Health.AggregatedHealthState -ne 'Ok') { throw 'Cluster health is not Ok' }; "
            "$Nodes=@(Get-ServiceFabricNode); "
            "if ($Nodes.Count -eq 0 -or @($Nodes | Where-Object { $_.NodeStatus -ne 'Up' }).Count) "
            "{ throw 'Not all cluster nodes are Up' }; Write-Output 'Cluster healthy; all nodes Up'"
        )
        result = record(run_dir, identifier + "-health-" + uuid.uuid4().hex[:8], "cleanup", 180, [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", command,
        ], scope="cluster")
        if result["exit_code"] != 0 or result["timed_out"]:
            raise ValueError("Cluster health check failed; subsequent candidates remain blocked.")
    (Path(run_dir) / "active.json").unlink()
    write_json(directory / "completed.json", {"ended": now()})
    summarize(run_dir)
    return {"candidate_id": identifier, "completed": True}


def summarize(run_dir):
    directory = Path(run_dir)
    plan = read_json(directory / "plan.json")
    results = []
    for candidate in plan["discovery"]["candidates"]:
        folder = directory / "candidates" / candidate["id"]
        item = {"id": candidate["id"], "completed": (folder / "completed.json").exists()}
        for key, filename in (
            ("reproduction", "result.json"), ("report", "report.json"),
            ("resolution", "resolution.json"), ("cleanup", "cleanup.json"),
        ):
            if (folder / filename).exists():
                item[key] = read_json(folder / filename)
        results.append(item)
    report = {**plan, "candidate_results": results}
    write_json(directory / "report.json", report)
    return report


def release_lock(state):
    lock = Path(state["lock"])
    released = lock.with_name("released-" + Path(state["run_dir"]).name)
    owner = lock / "owner.json"
    if owner.exists() and read_json(owner)["run_dir"] == state["run_dir"]:
        lock.rename(released)
    if released.exists():
        owner = released / "owner.json"
        if owner.exists():
            if read_json(owner)["run_dir"] != state["run_dir"]:
                raise RuntimeError("The released lock belongs to another audit.")
            owner.unlink()
        released.rmdir()


def acquire_lock(state):
    """Re-acquires the machine lock for an already-bootstrapped run."""
    lock = Path(state["lock"])
    try:
        lock.mkdir(parents=True)
    except FileExistsError:
        owner = lock / "owner.json"
        if not owner.exists() or read_json(owner)["run_dir"] != state["run_dir"]:
            raise RuntimeError("Another audit holds the machine lock; refusing to take it.")
        return
    write_json(lock / "owner.json", {"run_dir": state["run_dir"], "started": now()})


def reconcile(run_dir):
    """Resolves pending receipts left by an interrupted create.

    A receipt is only cleared when two inventory reads, separated by a settling
    delay, both show the issue's marker absent, so a create whose response was
    lost is never silently duplicated.
    """
    state = load_state(run_dir)
    receipts = Path(run_dir) / "receipts"
    pending = [
        path for path in sorted(receipts.glob("*.json"))
        if read_json(path).get("status") == "pending"
    ] if receipts.is_dir() else []
    if not pending:
        return {"reconciled": []}
    first = inventory(state)
    time.sleep(RECONCILE_SETTLE)
    second = inventory(state)
    outcomes = []
    for path in pending:
        marker = read_json(path).get("marker") or f"<!-- log-service-audit:{path.stem} -->"
        found = [item for item in first + second if marker in item["body"]]
        if found:
            match = found[0]
            if LABEL not in match["labels"]:
                raise RuntimeError("Previously submitted issue is missing needs triage; inspect it manually.")
            write_json(path, {"status": "published", "number": match["number"], "url": match["url"]})
            outcomes.append({"fingerprint": path.stem, "status": "published", "url": match["url"]})
            continue
        path.unlink()
        outcomes.append({"fingerprint": path.stem, "status": "cleared"})
    return {"reconciled": outcomes}


def republish(run_dir, reviewed=()):
    """Publishes approved findings whose publication stage failed, re-running no experiments.

    Duplicate review is refreshed rather than trusted: every issue filed since
    the run began must be one of this run's own publications, or named in
    ``reviewed`` by an operator who has compared it against the findings.
    """
    state = read_state(run_dir)
    acquire_lock(state)
    try:
        reconciled = reconcile(run_dir)["reconciled"]
        published = {
            read_json(path)["number"]
            for path in (Path(run_dir) / "receipts").glob("*.json")
            if read_json(path).get("status") == "published"
        }
        refreshed = snapshot(run_dir)
        started = datetime.fromisoformat(state["started"])
        unreviewed = sorted(
            item["number"] for item in read_json(state["issues_file"])
            if not item["is_pr"] and item["number"] not in published
            and item["number"] not in set(reviewed)
            and item["created_at"]
            and datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")) > started
        )
        if unreviewed:
            raise RuntimeError(
                "Issues filed since this run began need duplicate review before republication: "
                + ", ".join(f"#{number}" for number in unreviewed)
            )
        outcomes = []
        for path in sorted((Path(run_dir) / "candidates").glob("*/resolution.json")):
            identifier = path.parent.name
            if read_json(path).get("verdict") != "blocked":
                continue
            report = read_json(path.parent / "report.json")
            if not any(d["verdict"] == "approved" for d in report.get("verification", {}).get("decisions", [])):
                continue
            report["snapshot_digest"] = refreshed["digest"]
            outcome = publish(run_dir, report, identifier)
            write_json(path, outcome)
            outcomes.append({"id": identifier, "outcomes": outcome["outcomes"]})
    finally:
        release_lock(state)
    if (Path(run_dir) / "plan.json").exists():
        summarize(run_dir)
    return {"reconciled": reconciled, "reviewed": sorted(reviewed), "republished": outcomes}


def finish(run_dir):
    state = read_state(run_dir)
    completion = Path(run_dir) / "completed.json"
    if completion.exists():
        result = read_json(completion)["result"]
        release_lock(state)
        return result
    state = load_state(run_dir)
    report = summarize(run_dir)
    if any(not item["completed"] for item in report["candidate_results"]) or (Path(run_dir) / "active.json").exists():
        raise RuntimeError("Candidates remain unfinished; refusing to release the audit lock.")
    assert_clean(state)
    result = {
        "completed": True, "retained_worktree": state["worktree"],
        "report": str(Path(run_dir) / "report.json"), "coverage": report["discovery"]["coverage"],
        "environment_ready": report["preparation"]["environment_ready"],
        "blockers": report["preparation"]["blockers"], "candidates": report["candidate_results"],
    }
    write_json(completion, {"ended": now(), "result": result})
    release_lock(state)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap")
    for name in ("queue", "start-candidate", "handoff", "snapshot", "resolve", "complete",
                 "finish", "record", "preflight", "prepare", "shortlist", "block",
                 "reconcile", "republish"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", required=True)
        if name in ("start-candidate", "handoff", "resolve", "complete", "shortlist", "block"):
            command.add_argument("--candidate-id", required=True)
        if name == "block":
            command.add_argument("--reason", default="Stage failed before publication.")
        if name == "shortlist":
            command.add_argument("--limit", type=int, default=25)
        if name == "republish":
            command.add_argument("--reviewed", type=int, action="append", default=[])
        if name == "record":
            command.add_argument("--name", required=True)
            command.add_argument("--stage", choices=STAGES, required=True)
            command.add_argument("--timeout", type=int, default=600)
            command.add_argument("--scope", choices=("local", "cluster"), required=True)
            command.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.command == "bootstrap":
            output = bootstrap(json.load(sys.stdin))
        elif args.command == "record":
            argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            output = record(args.run_dir, args.name, args.stage, args.timeout, argv, args.scope)
        elif args.command == "snapshot":
            output = snapshot(args.run_dir)
        elif args.command == "queue":
            output = queue(args.run_dir, json.load(sys.stdin))
        elif args.command == "start-candidate":
            output = start_candidate(args.run_dir, args.candidate_id)
        elif args.command == "handoff":
            output = handoff(args.run_dir, args.candidate_id, json.load(sys.stdin))
        elif args.command == "resolve":
            payload = sys.stdin.read()
            output = resolve_candidate(args.run_dir, args.candidate_id, json.loads(payload) if payload.strip() else None)
        elif args.command == "complete":
            output = complete_candidate(args.run_dir, args.candidate_id, json.load(sys.stdin))
        elif args.command == "preflight":
            output = preflight(args.run_dir)
        elif args.command == "prepare":
            output = prepare(args.run_dir)
        elif args.command == "shortlist":
            output = shortlist(args.run_dir, args.candidate_id, args.limit)
        elif args.command == "block":
            output = block_candidate(args.run_dir, args.candidate_id, args.reason)
        elif args.command == "reconcile":
            output = reconcile(args.run_dir)
        elif args.command == "republish":
            output = republish(args.run_dir, args.reviewed)
        else:
            output = finish(args.run_dir)
        print(json.dumps(output))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
