"""Own review checkouts and fail closed when their identity or liveness is uncertain."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid


class LifecycleError(Exception):
    """A checkout cannot safely be created or removed."""


def git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise LifecycleError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout.strip()


def safe_path(value: str) -> Path:
    path = Path(os.path.abspath(os.path.expanduser(value)))
    if path != path.resolve():
        raise LifecycleError(f"Refusing a symlink or noncanonical path: {path}")
    return path


def _require_supported_platform(needs: str) -> None:
    if not sys.platform.startswith(("linux", "darwin")):
        raise LifecycleError(f"Automatic lifecycle operations require {needs}.")


def _ps(pid: int, *fields: str) -> list[str] | None:
    """Query macOS ps; None means the process does not exist."""
    result = subprocess.run(
        ["ps", "-o", ",".join(f"{field}=" for field in fields), "-p", str(pid)],
        capture_output=True, text=True, check=False, env={**os.environ, "LC_ALL": "C"},
    )
    out = result.stdout.strip()
    if result.returncode == 1 and not out and not result.stderr.strip():
        return None
    if result.returncode:
        raise LifecycleError(f"Cannot establish process identity for PID {pid}: ps failed.")
    return out.split(None, len(fields) - 1)


def _darwin_boot_id() -> str:
    # bootsessionuuid is stable; kern.boottime can shift when the clock is adjusted.
    for key in ("kern.bootsessionuuid", "kern.boottime"):
        result = subprocess.run(
            ["sysctl", "-n", key], capture_output=True, text=True, check=False
        )
        out = result.stdout.strip()
        if result.returncode == 0 and out:
            match = re.search(r"sec = (\d+)", out)
            return match.group(1) if match else out
    raise LifecycleError("Cannot establish local boot identity.")


def _darwin_process_identity(pid: int) -> dict[str, object] | None:
    fields = _ps(pid, "stat", "lstart")
    if fields is None:
        return None
    if len(fields) != 2:
        raise LifecycleError(f"Malformed process identity for PID {pid}.")
    state, started = fields
    if state.startswith("Z"):
        return None
    try:
        start = int(time.mktime(time.strptime(started, "%a %b %d %H:%M:%S %Y")))
    except (ValueError, OverflowError) as exc:
        raise LifecycleError(f"Malformed process start time for PID {pid}.") from exc
    # macOS has no PID namespaces, so a constant satisfies the stored-identity shape.
    return {"pid": pid, "start": str(start), "boot": _darwin_boot_id(), "namespace": 0}


def process_identity(pid: int) -> dict[str, object] | None:
    _require_supported_platform("Linux /proc or macOS ps process identity")
    if sys.platform.startswith("darwin"):
        return _darwin_process_identity(pid)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    except (OSError, IndexError) as exc:
        raise LifecycleError(f"Cannot establish process identity for PID {pid}: {exc}") from exc
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        namespace = Path("/proc/self/ns/pid").stat().st_ino
    except OSError as exc:
        raise LifecycleError(f"Cannot establish local boot/process namespace: {exc}") from exc
    if len(fields) < 20:
        raise LifecycleError(f"Malformed process identity for PID {pid}.")
    if fields[0] in {"Z", "X"}:
        return None
    return {"pid": pid, "start": fields[19], "boot": boot, "namespace": namespace}


def _parent_pid(pid: int) -> int:
    if sys.platform.startswith("darwin"):
        fields = _ps(pid, "ppid")
        if fields is None:
            raise LifecycleError(f"PID {pid} exited during ancestry inspection.")
        return int(fields[0])
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])


def ancestor_pids() -> set[int]:
    result: set[int] = set()
    pid = os.getppid()
    while pid > 1 and pid not in result:
        result.add(pid)
        try:
            pid = _parent_pid(pid)
        except (OSError, ValueError, IndexError) as exc:
            raise LifecycleError(f"Cannot establish runner ancestry: {exc}") from exc
    return result


def runner_context() -> dict[str, object]:
    if os.getppid() <= 1:
        raise LifecycleError("Cannot identify a persistent invoking process.")
    owner = process_identity(os.getppid())
    if owner is None:
        raise LifecycleError("The invoking process exited before ownership could be recorded.")
    run_id = os.environ.get("CONDUCTOR_SELF_RUN_ID") or os.environ.get("CONDUCTOR_RUN_ID", "")
    if run_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise LifecycleError("Conductor run identity is malformed.")
    home = Path(os.environ.get("CONDUCTOR_HOME", str(Path.home() / ".conductor")))
    return {
        "owner": owner,
        "run_id": run_id,
        "run_record": str((home / "runs" / f"{run_id}.json").absolute()) if run_id else "",
    }


def read_json(path: Path) -> dict:
    safe_path(str(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise LifecycleError(f"Not a private, user-owned regular file: {path}")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise LifecycleError(f"Expected an ownership object in {path}")
    return value


def check_inactive(record: dict, *, discard: bool) -> None:
    owner = record["owner"]
    if (
        not isinstance(owner, dict) or type(owner.get("pid")) is not int or owner["pid"] <= 1
        or not isinstance(owner.get("start"), str) or not owner["start"].isdigit()
        or not isinstance(owner.get("boot"), str) or not owner["boot"]
        or type(owner.get("namespace")) is not int
    ):
        raise LifecycleError("Missing or invalid owning process identity.")
    local = process_identity(os.getpid())
    if local is None or any(local[key] != owner[key] for key in ("boot", "namespace")):
        raise LifecycleError("Owner belongs to another boot or PID namespace; inspect manually.")
    current = process_identity(owner["pid"])
    ancestors = ancestor_pids()
    owner_alive = current is not None and current == owner
    if owner_alive and (discard or owner["pid"] not in ancestors):
        raise LifecycleError(
            f"Owning process PID {owner['pid']} is still active, possibly dashboard-only. "
            "Workflow inactivity is unproven; stop the runner/dashboard first."
        )

    run_id = record["run_id"]
    resumed_owner = False
    if run_id:
        run_path = safe_path(record["run_record"])
        if run_path.name != f"{run_id}.json":
            raise LifecycleError("Conductor run-record identity does not match ownership.")
        try:
            run = read_json(run_path)
        except FileNotFoundError:
            # Conductor removes its active record at exit; the original PID is checked separately.
            run = None
        if run is not None:
            if run.get("run_id") != run_id or type(run.get("pid")) is not int or run["pid"] <= 1:
                raise LifecycleError("Conductor run record is malformed; inactivity is unknown.")
            if process_identity(run["pid"]) is not None:
                same_run = os.environ.get("CONDUCTOR_SELF_RUN_ID") == run_id
                if discard or not same_run or run["pid"] not in ancestors:
                    raise LifecycleError(
                        f"Conductor run {run_id} has a live runner/dashboard (PID {run['pid']}). "
                        "Workflow inactivity is unproven; stop the runner/dashboard first."
                    )
                resumed_owner = True
    if not discard and not owner_alive and not resumed_owner:
        raise LifecycleError("Cleanup is not running under its owner; use explicit discard.")


@contextmanager
def registry(repo: str):
    """Serialize this repository's lifecycle commands; Linux or macOS is required."""
    _require_supported_platform("Linux or macOS with flock")
    import fcntl

    repo_path = safe_path(repo)
    if git(str(repo_path), "rev-parse", "--show-toplevel") != str(repo_path):
        raise LifecycleError("repo_root must name the checkout root.")
    common = safe_path(git(str(repo_path), "rev-parse", "--path-format=absolute", "--git-common-dir"))
    directory = safe_path(str(common / "pr-review-ownership"))
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise LifecycleError("Ownership registry must be user-owned and not writable by others.")
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise LifecycleError("Ownership lock must be a private, user-owned regular file.")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory, str(common)


def write_record(directory: Path, record: dict, *, create: bool = False) -> None:
    path = directory / f"{record['ownership_id']}.json"
    temporary = directory / f".{record['ownership_id']}-{uuid.uuid4().hex}.tmp"
    destination = path if create else temporary
    flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(destination, flags, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        if not create:
            safe_path(str(path))
            os.replace(temporary, path)
    except (LifecycleError, OSError) as exc:
        if not create and temporary.exists():
            try:
                temporary.unlink()
            except OSError as cleanup_error:
                raise LifecycleError(
                    f"{exc}; could not remove temporary ownership file {temporary}: {cleanup_error}"
                ) from exc
        raise


def validate_record(directory: Path, repo: str, path: str, branch: str | None) -> dict:
    target = safe_path(path)
    match = re.fullmatch(r"pr-([0-9]+)-([0-9a-f]{32})", target.name)
    manual = (
        "Retained. Stop its run, inspect git worktree list and git -C <worktree_path> status, "
        "then manually use git worktree remove <worktree_path> without --force."
    )
    if not match:
        raise LifecycleError(f"Legacy or unowned worktree: {target}. {manual}")
    ownership_id = match[2]
    try:
        record = read_json(directory / f"{ownership_id}.json")
    except FileNotFoundError as exc:
        raise LifecycleError(f"No ownership record for {target}. {manual}") from exc
    expected = {
        "version": 1,
        "repo_root": repo,
        "worktree_path": str(target),
        "ownership_id": ownership_id,
        "branch": f"pr-review/{match[1]}-{ownership_id}",
        "base_pin": f"refs/pr-review/{ownership_id}/base",
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise LifecycleError(f"Ownership identity does not match {target}. {manual}")
    if branch is not None and record["branch"] != branch:
        raise LifecycleError("Requested branch does not match the owned worktree.")
    for key in ("head_sha", "base_sha"):
        value = record.get(key)
        if not isinstance(value, str) or (value and not re.fullmatch(r"[0-9a-f]{40,64}", value)):
            raise LifecycleError(f"Invalid {key} in ownership record.")
    for key in ("run_id", "run_record", "common_dir", "base_ref"):
        if not isinstance(record.get(key), str):
            raise LifecycleError(f"Invalid {key} in ownership record.")
    return record


def ref_sha(repo: str, ref: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 1:
        return ""
    if result.returncode:
        raise LifecycleError(result.stderr.strip() or f"Cannot inspect {ref}")
    return result.stdout.strip()


def remove_owned(repo: str, record: dict, *, allow_dirty: bool = False) -> tuple[bool, bool]:
    """Delete only unchanged owned refs and a verified registered checkout."""
    target = safe_path(record["worktree_path"])
    if git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir") != record["common_dir"]:
        raise LifecycleError("Git common directory does not match ownership.")
    head_ref = f"refs/heads/{record['branch']}"
    refs = ((head_ref, record["head_sha"]), (record["base_pin"], record["base_sha"]))
    for ref, expected in refs:
        actual = ref_sha(repo, ref)
        if actual and (not expected or actual != expected):
            raise LifecycleError(f"Owned ref changed or has unknown provenance: {ref}. Retained.")
    entries = git(repo, "worktree", "list", "--porcelain", "-z").split("\0\0")
    registered = None
    for entry in entries:
        fields = entry.split("\0")
        if f"worktree {target}" in fields:
            registered = fields
        elif f"branch {head_ref}" in fields:
            raise LifecycleError("Owned branch is checked out at a different path. Retained.")
    if target.exists():
        if registered is None or f"branch {head_ref}" not in registered:
            raise LifecycleError("Path is not the registered owned worktree. Retained.")
        info = target.stat()
        if record.get("worktree_identity") != [info.st_dev, info.st_ino]:
            raise LifecycleError("Worktree directory identity changed or is unknown. Retained.")
        if git(str(target), "rev-parse", "--path-format=absolute", "--git-common-dir") != record["common_dir"]:
            raise LifecycleError("Worktree Git common directory changed. Retained.")
        if git(str(target), "rev-parse", "HEAD") != record["head_sha"]:
            raise LifecycleError("Worktree HEAD changed. Retained.")
        if git(str(target), "symbolic-ref", "HEAD") != head_ref:
            raise LifecycleError("Worktree branch changed. Retained.")
        dirty = git(str(target), "status", "--porcelain", "--untracked-files=all", "--ignored")
        if dirty and not allow_dirty:
            raise LifecycleError("Worktree is dirty (including ignored files). Retained; discard needs --allow-dirty.")
        args = ["worktree", "remove"]
        if allow_dirty:
            args.append("--force")
        git(repo, *args, str(target))
    elif registered is not None:
        raise LifecycleError("Registered worktree is missing; inspect manually. No refs deleted.")
    for ref, expected in refs:
        if ref_sha(repo, ref):
            git(repo, "update-ref", "-d", ref, expected)
    return not target.exists(), not ref_sha(repo, head_ref)


def cleanup_owned(repo: str, path: str, branch: str | None = None, *,
                  discard: bool = False, allow_dirty: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "ok": False, "worktree_removed": False, "branch_deleted": False,
        "worktree_path": path, "branch": branch or "", "head_sha": "", "base_sha": "", "notes": "",
    }
    record = None
    try:
        if allow_dirty and not discard:
            raise LifecycleError("Dirty removal requires explicit discard, never normal cleanup.")
        repo = str(safe_path(repo))
        with registry(repo) as (directory, _):
            record = validate_record(directory, repo, path, branch)
            result.update({key: record[key] for key in ("branch", "head_sha", "base_sha", "base_ref")})
            check_inactive(record, discard=discard)
            removed, deleted = remove_owned(repo, record, allow_dirty=allow_dirty)
            result.update(ok=True, worktree_removed=removed, branch_deleted=deleted)
            (directory / f"{record['ownership_id']}.json").unlink()
            result["notes"] = (
                "Discarded owned review checkout. Existing checkpoints are unusable."
                if discard else "Removed owned clean review checkout and unchanged refs."
            )
    except (LifecycleError, OSError, ValueError, KeyError) as exc:
        result["ok"] = False
        result["notes"] = str(exc)
        if record is not None:
            result["worktree_removed"] = not os.path.lexists(path)
            try:
                result["branch_deleted"] = not ref_sha(repo, f"refs/heads/{record['branch']}")
            except (LifecycleError, OSError):
                result["notes"] += " Could not verify remaining branch state."
    return result
