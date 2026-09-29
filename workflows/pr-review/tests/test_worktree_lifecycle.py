import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cleanup
import discard
import pr_worktree
import worktree_lifecycle as lifecycle


@unittest.skipUnless(sys.platform.startswith("linux"), "Lifecycle commands require Linux /proc")
class WorktreeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.worktrees = self.root / "repo.worktrees"
        self.worktrees.mkdir()
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Lifecycle Test")
        (self.repo / "tracked").write_text("base\n")
        self.git("add", "tracked")
        self.git("commit", "--quiet", "-m", "base")
        self.base_sha = self.git("rev-parse", "HEAD")
        self.git("checkout", "--quiet", "-b", "source")
        (self.repo / "tracked").write_text("head\n")
        self.git("commit", "--quiet", "-am", "head")
        self.head_sha = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/pull/570/head", self.head_sha)
        self.git("checkout", "--quiet", "main")
        self.git("remote", "add", "origin", str(self.repo))
        self.environment = patch.dict(os.environ, {
            "CONDUCTOR_SELF_RUN_ID": "", "CONDUCTOR_RUN_ID": "",
            "CONDUCTOR_HOME": str(self.root / "conductor"),
            "PR_REVIEW_GH_HOST": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "-C", str(cwd or self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def create(self, base="main"):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as stopped:
                pr_worktree.main([str(self.repo), str(self.worktrees), "570", "owner/repo", base])
        self.assertEqual(stopped.exception.code, 0)
        return json.loads(output.getvalue())

    def record_path(self, result):
        return self.repo / ".git" / "pr-review-ownership" / f"{result['ownership_id']}.json"

    def record(self, result):
        return json.loads(self.record_path(result).read_text())

    def update_record(self, result, **values):
        record = self.record(result)
        record.update(values)
        self.record_path(result).write_text(json.dumps(record))

    def clean(self, result, **kwargs):
        return lifecycle.cleanup_owned(
            str(self.repo), result["worktree_path"], result["branch"], **kwargs
        )

    def stop_owner(self, result):
        self.update_record(result, owner={**self.record(result)["owner"], "start": "0"})

    def test_unique_invocations_preserve_retained_and_legacy_checkout(self):
        legacy = self.worktrees / "pr-570"
        self.git("worktree", "add", "-b", "pr-review/570", str(legacy), self.head_sha)
        (legacy / "retained").write_text("do not discard")
        first = self.create()
        second = self.create()
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(first["worktree_path"], second["worktree_path"])
        self.assertNotEqual(first["branch"], second["branch"])
        self.assertEqual((legacy / "retained").read_text(), "do not discard")
        self.assertTrue(Path(first["worktree_path"]).exists())
        self.assertEqual(first["head_sha"], self.head_sha)
        self.assertEqual(first["base_sha"], self.base_sha)
        self.assertEqual(self.record(first)["repo_root"], str(self.repo))
        self.assertEqual(self.record(first)["owner"], lifecycle.process_identity(os.getppid()))

    def test_pinned_checkout_uses_https_helper_instead_of_ambient_ssh_identity(self):
        self.git("remote", "set-url", "origin", "git@github.com:owner/repo.git")
        config = (self.repo / ".git" / "config").read_text()
        original_git = pr_worktree.git
        fetches = []

        def local_transport(repo, *args):
            if "fetch" in args:
                fetches.append(args)
                self.assertEqual(args[-2], "https://github.com/owner/repo.git")
                self.assertIn("credential.helper=", args)
                self.assertIn("credential.helper=!gh auth git-credential", args)
                self.assertIn("http.https://github.com/owner/repo.git.extraHeader=", args)
                args = (*args[:-2], str(self.repo), args[-1])
            return original_git(repo, *args)

        with patch.dict(os.environ, {"PR_REVIEW_GH_HOST": "github.com"}), patch.object(
            pr_worktree, "git", side_effect=local_transport,
        ):
            created = self.create()
        self.assertTrue(created["ok"], created)
        self.assertEqual(len(fetches), 2)
        self.assertEqual(created["head_sha"], self.head_sha)
        self.assertEqual(created["base_sha"], self.base_sha)
        self.assertEqual(created["remote"], "origin")
        self.assertEqual((self.repo / ".git" / "config").read_text(), config)

    def test_clean_success_only_removes_owned_checkout_and_refs(self):
        first, second = self.create(), self.create()
        result = self.clean(first)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["worktree_removed"])
        self.assertTrue(result["branch_deleted"])
        self.assertEqual(result["head_sha"], self.head_sha)
        self.assertEqual(result["base_sha"], self.base_sha)
        self.assertFalse(Path(first["worktree_path"]).exists())
        self.assertFalse(self.record_path(first).exists())
        self.assertTrue(Path(second["worktree_path"]).exists())
        self.assertEqual(self.git("rev-parse", second["branch"]), self.head_sha)

    def test_legacy_cleanup_refuses_without_modifying(self):
        legacy = self.worktrees / "pr-570"
        self.git("worktree", "add", "-b", "pr-review/570", str(legacy), self.head_sha)
        result = lifecycle.cleanup_owned(str(self.repo), str(legacy), "pr-review/570")
        self.assertFalse(result["ok"])
        self.assertIn("manual", result["notes"])
        self.assertTrue(legacy.exists())
        self.assertEqual(self.git("rev-parse", "pr-review/570"), self.head_sha)

    def test_dirty_and_ignored_files_retained_by_normal_cleanup(self):
        for filename in ("tracked", "untracked", "ignored"):
            with self.subTest(filename=filename):
                created = self.create()
                path = Path(created["worktree_path"])
                if filename == "ignored":
                    (self.repo / ".git" / "info" / "exclude").write_text("ignored\n")
                (path / filename).write_text("keep this")
                result = self.clean(created)
                self.assertFalse(result["ok"])
                self.assertFalse(result["worktree_removed"])
                self.assertFalse(result["branch_deleted"])
                self.assertIn("--allow-dirty", result["notes"])
                self.assertEqual((path / filename).read_text(), "keep this")

    def test_discard_requires_acknowledgement(self):
        with contextlib.redirect_stderr(io.StringIO()), patch.object(discard, "cleanup_owned") as clean:
            with self.assertRaises(SystemExit) as stopped:
                discard.main([str(self.repo), str(self.worktrees / "pr-570")])
        self.assertEqual(stopped.exception.code, 2)
        clean.assert_not_called()

    def test_normal_cleanup_cannot_enable_dirty_removal(self):
        created = self.create()
        result = self.clean(created, allow_dirty=True)
        self.assertFalse(result["ok"])
        self.assertIn("requires explicit discard", result["notes"])
        self.assertTrue(Path(created["worktree_path"]).exists())

    def test_locked_worktree_is_retained_even_with_discard_approval(self):
        created = self.create()
        self.git("worktree", "lock", created["worktree_path"])
        self.stop_owner(created)
        result = self.clean(created, discard=True, allow_dirty=True)
        self.assertFalse(result["ok"])
        self.assertTrue(Path(created["worktree_path"]).exists())
        self.assertEqual(self.git("rev-parse", created["branch"]), self.head_sha)

    def test_discard_refuses_active_original_owner_even_with_dirty_approval(self):
        created = self.create()
        result = self.clean(created, discard=True, allow_dirty=True)
        self.assertFalse(result["ok"])
        self.assertIn("still active", result["notes"])
        self.assertTrue(Path(created["worktree_path"]).exists())

    def test_explicit_stopped_discard_requires_separate_dirty_approval(self):
        created = self.create()
        path = Path(created["worktree_path"])
        (path / "untracked").write_text("discard with approval only")
        self.stop_owner(created)
        refused = self.clean(created, discard=True)
        accepted = self.clean(created, discard=True, allow_dirty=True)
        self.assertFalse(refused["ok"])
        self.assertTrue(accepted["ok"], accepted)
        self.assertIn("checkpoints are unusable", accepted["notes"])
        self.assertFalse(path.exists())

    def test_discard_cli_reports_warning_and_nonzero_refusal(self):
        created = self.create()
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(
            io.StringIO()
        ) as warning:
            code = discard.main([
                str(self.repo), created["worktree_path"], "--acknowledge-discard",
            ])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])
        self.assertIn("checkpoints unusable", warning.getvalue())

    def test_resumed_live_runner_blocks_discard(self):
        created = self.create()
        run_id = "resumed"
        run_path = self.root / "resumed.json"
        run_path.write_text(json.dumps({"run_id": run_id, "pid": os.getpid()}))
        self.stop_owner(created)
        self.update_record(created, run_id=run_id, run_record=str(run_path))
        result = self.clean(created, discard=True)
        self.assertFalse(result["ok"])
        self.assertIn("Conductor run resumed has a live runner/dashboard", result["notes"])

    def test_missing_run_record_does_not_override_live_original_owner(self):
        created = self.create()
        self.update_record(created, run_id="gone", run_record=str(self.root / "gone.json"))
        result = self.clean(created, discard=True)
        self.assertFalse(result["ok"])
        self.assertTrue(Path(created["worktree_path"]).exists())

    def test_unreadable_liveness_fails_closed(self):
        created = self.create()
        with patch.object(lifecycle, "process_identity", side_effect=lifecycle.LifecycleError("unknown")):
            result = self.clean(created, discard=True)
        self.assertFalse(result["ok"])
        self.assertIn("unknown", result["notes"])
        self.assertTrue(Path(created["worktree_path"]).exists())

    def test_pid_reuse_is_not_original_owner(self):
        created = self.create()
        record = self.record(created)
        self.update_record(created, owner={**record["owner"], "start": "0"})
        result = self.clean(created, discard=True)
        self.assertTrue(result["ok"], result)

    def test_different_boot_or_namespace_refuses(self):
        for key, value in (("boot", "another-boot"), ("namespace", 0)):
            with self.subTest(key=key):
                created = self.create()
                self.update_record(created, owner={**self.record(created)["owner"], key: value})
                result = self.clean(created, discard=True)
                self.assertFalse(result["ok"])
                self.assertIn("another boot or PID namespace", result["notes"])
                self.assertTrue(Path(created["worktree_path"]).exists())

    def test_resumed_runner_can_perform_normal_cleanup(self):
        created = self.create()
        self.stop_owner(created)
        run_path = self.root / "resumed.json"
        run_path.write_text(json.dumps({"run_id": "resumed", "pid": os.getppid()}))
        self.update_record(created, run_id="resumed", run_record=str(run_path))
        with patch.dict(os.environ, {"CONDUCTOR_SELF_RUN_ID": "resumed"}):
            result = self.clean(created)
        self.assertTrue(result["ok"], result)

    def test_env_identity_alone_does_not_authorize_resumed_cleanup(self):
        created = self.create()
        self.stop_owner(created)
        self.update_record(created, run_id="resumed", run_record=str(self.root / "resumed.json"))
        with patch.dict(os.environ, {"CONDUCTOR_SELF_RUN_ID": "resumed"}):
            result = self.clean(created)
        self.assertFalse(result["ok"])
        self.assertTrue(Path(created["worktree_path"]).exists())

    def test_malformed_run_record_refuses_discard(self):
        created = self.create()
        self.stop_owner(created)
        run_path = self.root / "resumed.json"
        run_path.write_text('{"run_id":"resumed","pid":null}')
        self.update_record(created, run_id="resumed", run_record=str(run_path))
        result = self.clean(created, discard=True)
        self.assertFalse(result["ok"])
        self.assertIn("inactivity is unknown", result["notes"])

    def test_cleanup_outside_owner_requires_discard(self):
        created = self.create()
        with patch.object(lifecycle, "ancestor_pids", return_value=set()):
            result = self.clean(created)
        self.assertFalse(result["ok"])
        self.assertTrue(Path(created["worktree_path"]).exists())

    def test_ownership_repo_path_branch_and_head_mismatches_refuse(self):
        for key, value in (
            ("repo_root", "/not-this-repo"), ("worktree_path", "/not-this-path"),
            ("branch", "main"), ("head_sha", self.base_sha), ("base_sha", self.head_sha),
            ("worktree_identity", [0, 0]), ("common_dir", "/not-this-common-dir"),
        ):
            with self.subTest(key=key):
                created = self.create()
                self.update_record(created, **{key: value})
                result = self.clean(created)
                self.assertFalse(result["ok"], result)
                self.assertTrue(Path(created["worktree_path"]).exists())
                self.assertEqual(self.git("rev-parse", created["branch"]), self.head_sha)

    def test_changed_head_is_never_discarded_even_with_dirty_approval(self):
        created = self.create()
        path = Path(created["worktree_path"])
        self.git("commit", "--allow-empty", "-m", "valuable commit", cwd=path)
        self.stop_owner(created)
        result = self.clean(created, discard=True, allow_dirty=True)
        self.assertFalse(result["ok"])
        self.assertIn("changed", result["notes"])
        self.assertTrue(path.exists())

    def test_symlinked_worktree_and_ownership_record_refuse(self):
        created = self.create()
        path = Path(created["worktree_path"])
        moved = self.worktrees / "moved"
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
        result = self.clean(created)
        self.assertFalse(result["ok"])
        self.assertTrue(moved.exists())
        other = self.create()
        record_path = self.record_path(other)
        moved_record = record_path.with_suffix(".saved")
        record_path.rename(moved_record)
        record_path.symlink_to(moved_record)
        result = self.clean(other)
        self.assertFalse(result["ok"])
        self.assertTrue(Path(other["worktree_path"]).exists())

    def test_missing_record_refuses(self):
        created = self.create()
        self.record_path(created).unlink()
        result = self.clean(created)
        self.assertFalse(result["ok"])
        self.assertIn("No ownership", result["notes"])
        self.assertTrue(Path(created["worktree_path"]).exists())

    def test_partial_base_fetch_failure_tidies_only_own_branch(self):
        first = self.create()
        failed = self.create("nonexistent")
        self.assertFalse(failed["ok"])
        self.assertFalse(Path(failed["worktree_path"]).exists())
        self.assertEqual(lifecycle.ref_sha(str(self.repo), f"refs/heads/{failed['branch']}"), "")
        self.assertFalse(self.record_path(failed).exists())
        self.assertTrue(Path(first["worktree_path"]).exists())

    def test_partial_worktree_add_failure_tidies_refs(self):
        original = lifecycle.git

        def failing(repo, *args):
            if args[:2] == ("worktree", "add"):
                raise lifecycle.LifecycleError("simulated worktree add failure")
            return original(repo, *args)

        with patch.object(pr_worktree, "git", side_effect=failing):
            failed = self.create()
        self.assertFalse(failed["ok"])
        self.assertIn("simulated", failed["error"])
        self.assertEqual(lifecycle.ref_sha(str(self.repo), f"refs/heads/{failed['branch']}"), "")
        self.assertFalse(self.record_path(failed).exists())

    def test_failed_creation_does_not_hide_rollback_failure(self):
        original = lifecycle.git

        def failing(repo, *args):
            if args[:2] == ("worktree", "add"):
                raise lifecycle.LifecycleError("original add failure")
            return original(repo, *args)

        with patch.object(pr_worktree, "git", side_effect=failing), patch.object(
            pr_worktree, "remove_owned", side_effect=lifecycle.LifecycleError("rollback refused")
        ):
            result = self.create()
        self.assertFalse(result["ok"])
        self.assertIn("original add failure", result["error"])
        self.assertIn("rollback refused", result["error"])
        self.assertTrue(self.record_path(result).exists())
        self.assertEqual(self.git("rev-parse", result["branch"]), self.head_sha)

    def test_partial_cleanup_failure_reports_actual_progress_and_can_retry(self):
        created = self.create()
        original = lifecycle.git

        def failing(repo, *args):
            if args[:2] == ("update-ref", "-d"):
                raise lifecycle.LifecycleError("simulated ref deletion failure")
            return original(repo, *args)

        with patch.object(lifecycle, "git", side_effect=failing):
            failed = self.clean(created)
        self.assertFalse(failed["ok"])
        self.assertTrue(failed["worktree_removed"])
        self.assertFalse(failed["branch_deleted"])
        self.assertTrue(self.record_path(created).exists())
        retried = self.clean(created)
        self.assertTrue(retried["ok"], retried)

    def test_record_removal_failure_is_not_reported_as_success(self):
        created = self.create()
        original = Path.unlink
        record_path = self.record_path(created)

        def failing(path, *args, **kwargs):
            if path == record_path:
                raise PermissionError("record unlink denied")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", failing):
            result = self.clean(created)
        self.assertFalse(result["ok"])
        self.assertTrue(result["worktree_removed"])
        self.assertTrue(result["branch_deleted"])
        self.assertIn("record unlink denied", result["notes"])
        self.assertTrue(record_path.exists())

    def test_cleanup_invalid_input_is_honest(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cleanup.main([])
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertFalse(result["worktree_removed"])

    def test_non_linux_refuses_automatic_deletion(self):
        created = self.create()
        with patch.object(lifecycle.sys, "platform", "darwin"):
            result = self.clean(created)
        self.assertFalse(result["ok"])
        self.assertIn("Linux", result["notes"])
        self.assertTrue(Path(created["worktree_path"]).exists())


if __name__ == "__main__":
    unittest.main()
