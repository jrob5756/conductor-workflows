import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "audit.py"
spec = importlib.util.spec_from_file_location("log_service_audit", MODULE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        (self.worktree / "Wal.cs").write_text("class Wal {}", encoding="utf-8")
        self.directory = self.root / "run"
        self.directory.mkdir()
        self.lock = self.root / "cluster.lock"
        self.lock.mkdir()
        self.state = {
            "run_dir": str(self.directory), "worktree": str(self.worktree),
            "history_dir": str(self.directory.parent),
            "issues_file": str(self.directory / "issues.json"),
            "lock": str(self.lock), "sha": "a" * 40, "host": "example.ghe.com",
            "target": "example.ghe.com/azure-core-cto/log-service", "default_branch": "main",
            "options": {"publish": True, "environment": "redeploy", "max_candidates": 8},
        }
        audit.write_json(self.directory / "run.json", self.state)
        audit.write_json(self.lock / "owner.json", {"run_dir": str(self.directory)})
        audit.write_json(self.directory / "issues.json", [])
        self.candidate = {
            "id": "C001", "category": "bug", "title": "Append loses an acknowledged record",
            "path": "Wal.cs", "symbol": "Wal.Append", "invariant": "acknowledged records survive",
        }
        self.result = {
            "id": "C001", "status": "validated", "explanation": "An invariant fails.",
            "records": [
                self.evidence("control", "control", 0),
                self.evidence("repro1", "repro", 1),
                self.evidence("repro2", "repro", 1),
            ],
            "reproduction": "Run the supplied assertion twice.",
            "expected": "Record survives.", "actual": "Record disappears.",
            "impact": "Acknowledged data loss.", "reachability": "Called from Append.",
            "patch": "",
        }
        self.decision = {
            "id": "C001", "verdict": "approved", "reason": "Checked assertions and all issues.",
            "duplicate_number": 0, "evidence_checked": True, "secrets_checked": True,
        }
        self.report = {
            "preparation": {"environment_ready": True, "blockers": []},
            "discovery": {"candidates": [self.candidate], "coverage": "Wal"},
            "reproduction": {"results": [self.result]},
            "verification": {"decisions": [self.decision]},
            "snapshot_digest": audit.digest([]),
        }

    def evidence(self, name, stage, code, diff=""):
        directory = self.directory / "records" / name
        directory.mkdir(parents=True, exist_ok=True)
        for filename, content in (
            ("stdout.txt", "Actual execution output"),
            ("stderr.txt", ""), ("before.patch", diff),
        ):
            (directory / filename).write_text(content, encoding="utf-8")
        audit.write_json(directory / "record.json", {
            "name": name, "stage": stage, "exit_code": code,
            "argv": ["dotnet", "test"], "cwd": self.state["worktree"],
            "sha": self.state["sha"], "started": audit.now(), "ended": audit.now(),
            "seconds": 1, "timed_out": False, "candidate_id": "C001",
            "artifacts": {
                name: audit.file_digest(directory / name)
                for name in ("stdout.txt", "stderr.txt", "before.patch")
            },
        })
        return str((directory / "record.json").relative_to(self.directory))

    def validate(self):
        return audit.validate_report(self.state, self.report, [])

    def marker(self):
        return f"<!-- log-service-audit:{audit.fingerprint(self.candidate)} -->"

    def issue(self, number=42, marker=None, labels=None):
        return {
            "number": number, "title": "Previously found defect",
            "body": marker if marker is not None else self.marker(),
            "state": "closed", "url": f"https://example.ghe.com/azure-core-cto/log-service/issues/{number}",
            "is_pr": False, "labels": ["needs triage"] if labels is None else labels,
        }

    def test_bug_requires_control_and_two_distinct_failing_executions(self):
        self.assertEqual(len(self.validate()), 1)
        self.result["records"].pop()
        with self.assertRaisesRegex(ValueError, "two failing"):
            self.validate()
        self.result["records"].append(self.result["records"][-1])
        with self.assertRaisesRegex(ValueError, "same evidence"):
            self.validate()

    def test_path_alias_cannot_count_as_second_execution(self):
        self.result["records"][-1] = str(Path("records") / "repro1" / ".." / "repro1" / "record.json")
        with self.assertRaisesRegex(ValueError, "same evidence"):
            self.validate()

    def test_missing_control_blocks_publication(self):
        self.result["records"] = self.result["records"][1:]
        with self.assertRaisesRegex(ValueError, "passing control"):
            self.validate()

    def test_tampering_and_wrong_sha_are_rejected(self):
        path = self.directory / self.result["records"][0]
        original = audit.read_json(path)
        for key, value in (("sha", "b" * 40), ("exit_code", False)):
            modified = {**original, key: value}
            audit.write_json(path, modified)
            with self.assertRaisesRegex(ValueError, "Invalid or stale"):
                self.validate()
        audit.write_json(path, original)
        (path.parent / "stdout.txt").write_text("fabricated pass", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed after recording"):
            self.validate()

    def test_timed_out_control_cannot_satisfy_the_control_requirement(self):
        path = self.directory / self.result["records"][0]
        audit.write_json(path, {**audit.read_json(path), "timed_out": True})
        with self.assertRaisesRegex(ValueError, "passing control"):
            self.validate()

    def test_timed_out_repro_cannot_count_toward_two_failing_attempts(self):
        path = self.directory / self.result["records"][1]
        audit.write_json(path, {**audit.read_json(path), "timed_out": True})
        with self.assertRaisesRegex(ValueError, "two failing"):
            self.validate()

    def test_superseded_timed_out_record_may_still_be_listed_for_transparency(self):
        superseded = self.evidence("control-attempt1-timed-out", "control", 1)
        path = self.directory / superseded
        audit.write_json(path, {**audit.read_json(path), "timed_out": True})
        self.result["records"].append(superseded)
        self.assertEqual(len(self.validate()), 1)

    def test_missing_and_outside_artifacts_are_rejected(self):
        self.result["records"][0] = "..\\outside.json" if sys.platform == "win32" else "../outside.json"
        with self.assertRaisesRegex(ValueError, "must be inside"):
            self.validate()
        self.result["records"][0] = "missing.json"
        with self.assertRaises(FileNotFoundError):
            self.validate()

    def test_decision_must_cover_every_candidate(self):
        self.report["verification"]["decisions"] = []
        with self.assertRaisesRegex(ValueError, "Every candidate"):
            self.validate()

    def test_duplicate_ids_and_invalid_verdict_are_rejected(self):
        self.report["verification"]["decisions"].append(copy.deepcopy(self.decision))
        with self.assertRaisesRegex(ValueError, "repeated"):
            self.validate()
        self.report["verification"]["decisions"].pop()
        self.decision["verdict"] = "probably"
        with self.assertRaisesRegex(ValueError, "Invalid verification"):
            self.validate()

    def test_approval_cannot_override_blocked_reproduction_or_missing_review(self):
        for field in ("evidence_checked", "secrets_checked"):
            self.decision[field] = False
            with self.assertRaisesRegex(ValueError, "both verification"):
                self.validate()
            self.decision[field] = True
        self.result["status"] = "blocked"
        with self.assertRaisesRegex(ValueError, "both verification"):
            self.validate()

    def test_dead_code_needs_removal_build_tests_and_reachability(self):
        self.candidate["category"] = "dead_code"
        self.result["records"] = [
            self.evidence("baseline", "baseline", 0),
            self.evidence("build", "removed-build", 0, "-unused code"),
            self.evidence("tests", "removed-test", 0, "-unused code"),
        ]
        self.result["patch"] = "removal.patch"
        (self.directory / "removal.patch").write_text("-unused code", encoding="utf-8")
        self.assertEqual(len(self.validate()), 1)
        self.result["reachability"] = ""
        with self.assertRaisesRegex(ValueError, "reachability"):
            self.validate()
        self.result["reachability"] = "No static or dynamic consumers."
        self.result["records"][-1] = self.evidence("unchanged-tests", "removed-test", 0)
        with self.assertRaisesRegex(ValueError, "deletion diff"):
            self.validate()

    def test_semantic_duplicate_requires_real_issue_number(self):
        self.decision["verdict"] = "duplicate"
        self.decision["duplicate_number"] = 42
        with self.assertRaisesRegex(ValueError, "inventory issue"):
            self.validate()
        entries = [self.issue(marker="Manually filed root cause without marker.")]
        self.report["snapshot_digest"] = audit.digest(entries)
        self.assertEqual(audit.validate_report(self.state, self.report, entries), [])

    def test_blocked_candidate_still_cleans_up_and_lets_the_run_continue(self):
        self.make_plan(2)
        self.start()
        outcome = audit.block_candidate(self.directory, "C001", "Publication gate refused the evidence.")
        self.assertEqual(outcome["verdict"], "blocked")
        self.assertIn("Publication gate", outcome["reason"])
        self.complete()
        self.assertEqual(self.start("C002")["candidate"]["id"], "C002")

    def test_blocking_never_overwrites_a_real_resolution(self):
        self.make_plan()
        self.start()
        self.decline()
        before = audit.read_json(self.directory / "candidates" / "C001" / "resolution.json")
        audit.block_candidate(self.directory, "C001", "late block")
        self.assertEqual(audit.read_json(self.directory / "candidates" / "C001" / "resolution.json"), before)

    def test_blocked_candidates_appear_in_the_final_report(self):
        self.make_plan()
        self.start()
        audit.block_candidate(self.directory, "C001", "Inventory refresh failed.")
        self.complete()
        with patch.object(audit, "assert_clean"):
            report = audit.finish(self.directory)
        self.assertEqual(report["candidates"][0]["resolution"]["verdict"], "blocked")

    def test_shortlist_ranks_by_overlap_and_points_at_the_full_inventory(self):
        entries = [
            {**self.issue(number=10, marker="Unrelated caching change"), "title": "Cache eviction tuning"},
            {**self.issue(number=11, marker="Wal.Append loses acknowledged records on failover"),
             "title": "Append drops acknowledged records"},
        ]
        audit.write_json(self.directory / "issues.json", entries)
        self.make_plan()
        result = audit.shortlist(self.directory, "C001")
        self.assertEqual(result["inventory_count"], 2)
        self.assertEqual(result["shortlist"][0]["number"], 11)
        self.assertEqual(result["inventory_file"], self.state["issues_file"])
        self.assertTrue((self.directory / "candidates" / "C001" / "shortlist.json").is_file())

    def test_shortlist_is_not_inlined_into_published_issues(self):
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        audit.write_json(folder / "shortlist.json", {"shortlist": []})
        (folder / "Repro.cs").write_text("class Repro { void X() { } }", encoding="utf-8")
        body = self.body()
        self.assertIn("Repro.cs", body)
        self.assertNotIn("shortlist.json", body)

    def test_preflight_reports_blockers_without_running_agents(self):
        with patch.object(audit.shutil, "which", return_value=None), patch.object(
            audit.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 1, "", "module missing"),
        ):
            result = audit.preflight(self.directory)
        self.assertFalse(result["ok"])
        self.assertTrue(any("tool:git" in b for b in result["blockers"]))
        self.assertTrue(any("authenticode" in b for b in result["blockers"]))
        self.assertTrue((self.directory / "preflight.json").is_file())

    def test_preflight_passes_when_toolchain_is_sound(self):
        with patch.object(audit.shutil, "which", return_value=r"C:\tool.exe"), patch.object(
            audit.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, "Valid", ""),
        ):
            result = audit.preflight(self.directory)
        self.assertTrue(result["ok"], result["blockers"])

    def test_preflight_requires_an_installed_cluster_only_for_existing_mode(self):
        with patch.object(audit.shutil, "which", return_value=r"C:\tool.exe"), patch.object(
            audit.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "Valid", ""),
        ), patch.object(audit.Path, "is_dir", return_value=False):
            self.state["options"]["environment"] = "existing"
            audit.write_json(self.directory / "run.json", self.state)
            strict = audit.preflight(self.directory)
            self.state["options"]["environment"] = "reinstall"
            audit.write_json(self.directory / "run.json", self.state)
            lenient = audit.preflight(self.directory)
        self.assertFalse(strict["ok"])
        self.assertTrue(lenient["ok"], lenient["blockers"])

    def test_run_summary_excludes_the_duplicated_script_payload(self):
        summary = audit.run_summary(self.state)
        self.assertNotIn("stdout", summary)
        self.assertNotIn("stderr", summary)
        self.assertEqual(summary["run_dir"], self.state["run_dir"])
        self.assertEqual(summary["options"], self.state["options"])

    def measurement(self, **overrides):
        return {
            "metric": "append throughput", "unit": "ops/s",
            "scenario": "Tests.EndToEnd Scenario 1, 1000 ops, concurrency 16",
            "baseline_value": 970, "observed_value": 610, "samples": 3,
            "threshold": "AGENTS.md treats a drop over 5% as a regression",
            **overrides,
        }

    def performance_result(self, **overrides):
        self.candidate["category"] = "performance"
        self.result.update(
            records=[
                self.evidence("perf-baseline", "baseline", 0),
                self.evidence("perf-run1", "measurement", 0),
                self.evidence("perf-run2", "measurement", 0),
            ],
            measurement=self.measurement(**overrides),
        )
        return self.result

    def test_performance_needs_a_reference_and_repeated_measurements(self):
        self.performance_result()
        self.assertEqual(len(self.validate()), 1)

    def test_successful_measurements_are_not_treated_as_failing_reproductions(self):
        self.performance_result()
        records = self.result["records"]
        self.result["records"] = [records[0], records[1]]
        with self.assertRaisesRegex(ValueError, "two successful measurement runs"):
            self.validate()

    def test_performance_without_a_reference_measurement_is_rejected(self):
        self.performance_result()
        self.result["records"] = self.result["records"][1:]
        with self.assertRaisesRegex(ValueError, "reference measurement"):
            self.validate()

    def test_performance_claim_must_carry_its_numbers(self):
        self.performance_result()
        del self.result["measurement"]
        with self.assertRaisesRegex(ValueError, "measurement object"):
            self.validate()
        for field in ("metric", "unit", "scenario", "threshold"):
            self.performance_result(**{field: "  "})
            with self.assertRaisesRegex(ValueError, f"missing {field}"):
                self.validate()
        for field in ("baseline_value", "observed_value", "samples"):
            self.performance_result(**{field: "fast"})
            with self.assertRaisesRegex(ValueError, f"numeric {field}"):
                self.validate()

    def test_single_sample_or_no_difference_is_not_a_regression(self):
        self.performance_result(samples=1)
        with self.assertRaisesRegex(ValueError, "at least two samples"):
            self.validate()
        self.performance_result(observed_value=970)
        with self.assertRaisesRegex(ValueError, "not a regression"):
            self.validate()

    def test_performance_issue_publishes_its_measurement_table(self):
        self.performance_result()
        body = self.body()
        self.assertIn("## Measurement", body)
        self.assertIn("append throughput", body)
        self.assertIn("| **Reference** | 970 |", body)
        self.assertIn("| **Observed** | 610 |", body)
        self.assertIn("-37.1%", body)
        self.assertIn("Samples per side", body)

    def test_non_performance_findings_omit_the_measurement_table(self):
        self.assertNotIn("## Measurement", self.body())

    def test_performance_is_a_recognised_discovery_category(self):
        self.assertEqual(audit.CATEGORIES["performance"], "P")
        plan = self.make_plan()
        plan["discoveries"]["performance"]["candidates"] = [
            {**self.candidate, "id": "P001", "category": "performance"}
        ]
        plan["discovery"]["mapping"].append({
            "source_id": "P001", "disposition": "deferred", "candidate_id": "",
            "reason": "Needs a healthy cluster to measure.",
        })
        audit.validate_plan(self.state, plan)

    def test_prepare_passes_the_slice_array_the_deploy_script_accepts(self):
        """-File flattens arrays into one string, which the script's validation rejects."""
        calls = []
        with patch.object(audit, "record", side_effect=lambda rd, name, stage, timeout, argv, scope: (
            calls.append((name, argv)) or {"exit_code": 0, "timed_out": False, "seconds": 1,
                                           "record": f"records\\{name}\\record.json"}
        )), patch.object(audit, "collect_provenance", return_value={}), patch.object(
            audit, "worktree_status", return_value="",
        ):
            self.state["options"]["environment"] = "redeploy"
            audit.write_json(self.directory / "run.json", self.state)
            audit.prepare(self.directory)
        argv = next(a for name, a in calls if name == "deploy-logservice")
        joined = " ".join(argv)
        self.assertNotIn("slice1,slice2,slice3", joined)
        self.assertIn("@('slice1','slice2','slice3')", joined)
        self.assertIn("-WaitForHealthy", joined)

    def test_prepare_marks_environment_unready_when_a_deploy_fails(self):
        def fake_record(rd, name, stage, timeout, argv, scope):
            failed = name == "deploy-logservice"
            return {"exit_code": 1 if failed else 0, "timed_out": False, "seconds": 1,
                    "record": f"records\\{name}\\record.json"}

        with patch.object(audit, "record", side_effect=fake_record), patch.object(
            audit, "collect_provenance", return_value={},
        ), patch.object(audit, "worktree_status", return_value=""):
            result = audit.prepare(self.directory)
        self.assertFalse(result["environment_ready"])
        self.assertTrue(any("deploy-logservice" in b for b in result["blockers"]))
        names = [s["name"] for s in result["steps"]]
        self.assertNotIn("smoke", names)
        self.assertNotIn("deploy-sampleapp", names)

    def test_prepare_reports_ready_when_every_required_step_succeeds(self):
        with patch.object(audit, "record", side_effect=lambda rd, name, stage, timeout, argv, scope: {
            "exit_code": 0, "timed_out": False, "seconds": 1, "record": f"records\\{name}\\record.json",
        }), patch.object(audit, "collect_provenance", return_value={"sha": self.state["sha"]}), patch.object(
            audit, "worktree_status", return_value="",
        ):
            result = audit.prepare(self.directory)
        self.assertTrue(result["environment_ready"], result["blockers"])
        self.assertIn("smoke", [s["name"] for s in result["steps"]])
        self.assertTrue((self.directory / "preparation.json").is_file())

    def test_cluster_health_gates_readiness_but_smoke_only_warns(self):
        """Readiness means live experiments can run; smoke failing is reported, not fatal."""
        def fake_record(rd, name, stage, timeout, argv, scope):
            return {"exit_code": 1 if name == "smoke" else 0, "timed_out": False,
                    "seconds": 1, "record": f"records\\{name}\\record.json"}

        with patch.object(audit, "record", side_effect=fake_record), patch.object(
            audit, "collect_provenance", return_value={},
        ), patch.object(audit, "worktree_status", return_value=""):
            result = audit.prepare(self.directory)
        self.assertTrue(result["environment_ready"], result["blockers"])
        self.assertEqual(result["blockers"], [])
        self.assertTrue(any("smoke" in w for w in result["warnings"]))
        self.assertIn("non-blocking failures", result["summary"])
        self.assertIn("cluster-health", [s["name"] for s in result["steps"]])

    def test_unhealthy_cluster_does_block_readiness(self):
        def fake_record(rd, name, stage, timeout, argv, scope):
            return {"exit_code": 1 if name == "cluster-health" else 0, "timed_out": False,
                    "seconds": 1, "record": f"records\\{name}\\record.json"}

        with patch.object(audit, "record", side_effect=fake_record), patch.object(
            audit, "collect_provenance", return_value={},
        ), patch.object(audit, "worktree_status", return_value=""):
            result = audit.prepare(self.directory)
        self.assertFalse(result["environment_ready"])
        self.assertTrue(any("cluster-health" in b for b in result["blockers"]))
        self.assertNotIn("smoke", [s["name"] for s in result["steps"]])

    def test_a_cleaner_worktree_than_baseline_is_accepted(self):
        """Cleanup restoring more than it touched must not fail the completion gate."""
        baseline = " M ref/Data.Impl.dll\n M ref/Data.Interfaces.V2.dll"
        (self.directory / "worktree-baseline.txt").write_text(baseline, encoding="utf-8")
        with patch.object(audit, "run", return_value=self.state["sha"]), patch.object(
            audit, "worktree_status", return_value="",
        ):
            audit.assert_clean(self.state)

    def test_preparations_own_footprint_does_not_block_the_queue(self):
        """Deploying touches tracked reference DLLs; that must not look like candidate dirt."""
        baseline = " M src/csharp/.../Microsoft.ServiceFabric.Data.Impl.dll"
        (self.directory / "worktree-baseline.txt").write_text(baseline, encoding="utf-8")
        with patch.object(audit, "run", side_effect=[self.state["sha"], baseline]):
            audit.assert_clean(self.state)

    def test_changes_beyond_the_baseline_are_still_rejected(self):
        baseline = " M src/csharp/.../Data.Impl.dll"
        (self.directory / "worktree-baseline.txt").write_text(baseline, encoding="utf-8")
        dirty = baseline + "\n?? tests/integration/LeftoverReproTest.cs"
        with patch.object(audit, "run", side_effect=[self.state["sha"], dirty]):
            with self.assertRaisesRegex(ValueError, "beyond the preparation baseline"):
                audit.assert_clean(self.state)

    def test_missing_baseline_still_requires_a_pristine_worktree(self):
        with patch.object(audit, "run", side_effect=[self.state["sha"], " M something.cs"]):
            with self.assertRaisesRegex(ValueError, "beyond the preparation baseline"):
                audit.assert_clean(self.state)
        with patch.object(audit, "run", side_effect=[self.state["sha"], ""]):
            audit.assert_clean(self.state)

    def test_prepare_records_its_worktree_footprint(self):
        with patch.object(audit, "record", side_effect=lambda rd, name, stage, timeout, argv, scope: {
            "exit_code": 0, "timed_out": False, "seconds": 1, "record": f"records\\{name}\\record.json",
        }), patch.object(audit, "collect_provenance", return_value={}), patch.object(
            audit, "worktree_status", return_value=" M ref/Data.Impl.dll",
        ):
            result = audit.prepare(self.directory)
        self.assertEqual(result["worktree_baseline"], [" M ref/Data.Impl.dll"])
        self.assertEqual(
            (self.directory / "worktree-baseline.txt").read_text(encoding="utf-8"),
            " M ref/Data.Impl.dll",
        )

    def test_windows_powershell_children_drop_only_core_module_paths(self):
        core = r"C:\Program Files\PowerShell\7\Modules"
        user_core = r"C:\Users\me\Documents\PowerShell\Modules"
        global_core = r"C:\Program Files\PowerShell\Modules"
        keep = [
            r"C:\Users\me\Documents\WindowsPowerShell\Modules",
            r"C:\Program Files\WindowsPowerShell\Modules",
            r"C:\Windows\system32\WindowsPowerShell\v1.0\Modules",
            r"C:\Program Files\Microsoft SDKs\Service Fabric\Tools\PSModule",
        ]
        original = os.pathsep.join([user_core, global_core, core] + keep)
        with patch.dict(os.environ, {"PSModulePath": original}):
            env = audit.child_environment(["powershell.exe", "-File", "x.ps1"])
            self.assertEqual(env["PSModulePath"].split(os.pathsep), keep)
            self.assertIsNone(audit.child_environment(["pwsh.exe", "-File", "x.ps1"]))
            self.assertIsNone(audit.child_environment(["dotnet", "test"]))

    def test_child_environment_left_untouched_when_nothing_to_strip(self):
        clean = os.pathsep.join([r"C:\Windows\system32\WindowsPowerShell\v1.0\Modules"])
        with patch.dict(os.environ, {"PSModulePath": clean}):
            self.assertIsNone(audit.child_environment(["powershell.exe", "-File", "x.ps1"]))

    def test_patch_resolves_by_bare_filename_or_run_relative_path(self):
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        (folder / "repro.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
        for reference in ("repro.patch", "candidates\\C001\\repro.patch"):
            with self.subTest(reference=reference):
                self.result["patch"] = reference
                self.assertEqual(len(self.validate()), 1)

    def test_unresolvable_patch_does_not_discard_a_verified_bug(self):
        self.result["patch"] = "never-saved.patch"
        self.assertEqual(len(self.validate()), 1)

    def test_empty_patch_file_is_still_rejected(self):
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        (folder / "empty.patch").write_text("   \n", encoding="utf-8")
        self.result["patch"] = "empty.patch"
        with self.assertRaisesRegex(ValueError, "patch is empty"):
            self.validate()

    def test_dead_code_still_requires_a_resolvable_deletion_patch(self):
        self.candidate["category"] = "dead_code"
        self.result["records"] = [
            self.evidence("baseline", "baseline", 0),
            self.evidence("build", "removed-build", 0, "-unused"),
            self.evidence("tests", "removed-test", 0, "-unused"),
        ]
        self.result["patch"] = "missing-deletion.patch"
        with self.assertRaisesRegex(ValueError, "resolvable deletion patch"):
            self.validate()

    def test_secret_in_title_blocks_before_network(self):
        self.candidate["title"] = "leaked ghp_" + "a" * 30
        with patch.object(audit, "inventory") as network:
            with self.assertRaisesRegex(ValueError, "credential"):
                audit.publish(self.directory, self.report)
        network.assert_not_called()

    def body(self, environment_ready=None):
        evidence = audit.check_evidence(self.state, self.candidate, self.result)
        return audit.issue_body(
            self.state, self.candidate, self.result, self.decision,
            audit.fingerprint(self.candidate), evidence, environment_ready,
        )

    def test_issue_inlines_saved_artifacts_so_it_is_self_contained(self):
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        (folder / "ReproTest.cs").write_text("class ReproTest { }", encoding="utf-8")
        audit.write_json(folder / "result.json", self.result)
        body = self.body()
        self.assertIn("ReproTest.cs", body)
        self.assertIn("```csharp\nclass ReproTest { }\n```", body)
        self.assertNotIn("result.json", body)

    def test_issue_never_leaks_machine_local_paths_or_recorder_commands(self):
        self.result["reproduction"] = (
            f"Sources at candidates\\C001\\ReproTest.cs and {self.state['worktree']}\\src\\Wal.cs.\n"
            'Run: python "C:\\tools\\audit.py" record --run-dir "<run-dir>" --name x '
            "--stage repro --scope local --timeout 600 -- dotnet test tests\\Wal.Tests.csproj"
        )
        self.result["actual"] = f"Failed under {self.state['run_dir']}\\records\\repro1\\record.json"
        body = self.body()
        for leaked in (self.state["run_dir"], self.state["worktree"], "audit.py", "<run-dir>", "--scope"):
            self.assertNotIn(leaked, body)
        self.assertIn("dotnet test tests\\Wal.Tests.csproj", body)
        self.assertIn("src\\Wal.cs", body)
        self.assertIn("`repro1`", body)

    def test_commands_come_from_recorded_argv_not_prose(self):
        body = self.body()
        self.assertIn("### Commands", body)
        self.assertIn("# control (control, exit 0)\ndotnet test", body)
        self.assertIn("# repro1 (repro, exit 1)", body)

    def test_environment_claim_matches_actual_readiness(self):
        self.assertIn("no live Service Fabric cluster", self.body(environment_ready=False))
        self.assertIn("disposable local Service Fabric cluster", self.body(environment_ready=True))

    def test_long_analysis_is_collapsed_and_marker_retained(self):
        body = self.body()
        self.assertTrue(body.startswith(f"<!-- log-service-audit:{audit.fingerprint(self.candidate)} -->"))
        self.assertIn("<summary>Independent verification and duplicate review</summary>", body)
        self.assertIn("<summary>Reachability and compatibility analysis</summary>", body)
        self.assertIn("## Summary", body)
        self.assertIn("## Reproduction", body)

    def test_oversized_artifacts_are_dropped_rather_than_failing_publication(self):
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        (folder / "Huge.cs").write_text("x" * (audit.MAX_BODY + 5000), encoding="utf-8")
        body = self.body()
        self.assertLessEqual(len(body), audit.MAX_BODY)
        self.assertIn("omitted to fit GitHub's size limit", body)

    def test_audit_internal_locations_and_recorder_jargon_are_removed(self):
        self.result["explanation"] = (
            "All work was in-process and recorded with --scope local; no cluster command ran."
        )
        self.result["reachability"] = (
            "My test source is retained under the run directory at candidates\\C002\\review\\ "
            "alongside the four records I created."
        )
        body = self.body()
        for leaked in ("--scope", "run directory", "candidates\\C002"):
            self.assertNotIn(leaked, body)
        self.assertIn("recorded locally", body)
        self.assertIn("this audit's local evidence bundle", body)

    def test_only_inlined_artifacts_are_labelled_as_inlined(self):
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        (folder / "Repro.cs").write_text("class Repro { void X() { } }", encoding="utf-8")
        (folder / "Repro.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
        self.result["reproduction"] = (
            "See candidates\\C001\\Repro.cs and candidates\\C001\\Repro.patch."
        )
        body = self.body()
        self.assertIn("`Repro.cs` (inlined below)", body)
        self.assertIn("`Repro.patch`", body)
        self.assertNotIn("`Repro.patch` (inlined below)", body)

    def test_inlined_artifact_source_is_not_reformatted_as_prose(self):
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        source = "class Repro {\n  void Run() {\n    Assert.True(false);\n  }\n}"
        (folder / "Repro.cs").write_text(source, encoding="utf-8")
        body = self.body()
        self.assertIn(f"```csharp\n{source}\n```", body)

    def test_indented_code_in_prose_is_fenced_so_it_renders_as_code(self):
        self.result["reproduction"] = (
            "Add this test:\n\n"
            "  [Fact]\n"
            "  public void Repro() {\n"
            "      Assert.True(false);\n"
            "  }\n\n"
            "Then run it."
        )
        body = self.body()
        self.assertIn("```\n[Fact]\npublic void Repro() {\n    Assert.True(false);\n}\n```", body)
        self.assertIn("Then run it.", body)

    def test_indented_prose_that_is_not_code_is_left_alone(self):
        self.result["impact"] = "Consequences:\n\n  first consequence\n  second consequence\n"
        body = self.body()
        self.assertIn("first consequence", body)
        self.assertNotIn("```\nfirst consequence", body)

    def test_artifact_reference_keeps_sentence_punctuation(self):
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        (folder / "Repro.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
        self.result["reproduction"] = "See candidates\\C001\\Repro.patch. Then apply it."
        body = self.body()
        self.assertIn("`Repro.patch` (inlined below). Then apply it.", body)

    def test_stale_run_directory_phrasing_is_removed(self):
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        (folder / "Repro.cs").write_text("class Repro { void X() { } }", encoding="utf-8")
        self.result["reproduction"] = "Saved artifacts (relative to run dir): candidates\\C001\\Repro.cs"
        body = self.body()
        self.assertNotIn("run dir", body)
        self.assertIn("Saved artifacts: `Repro.cs` (inlined below)", body)

    def test_closed_marker_match_suppresses_create(self):
        with patch.object(audit, "inventory", return_value=[self.issue()]), patch.object(audit, "run") as gh:
            result = audit.publish(self.directory, self.report)
        self.assertEqual(result["outcomes"][0]["status"], "duplicate")
        gh.assert_not_called()

    def test_new_issue_after_review_stops_before_any_mutation(self):
        with patch.object(audit, "inventory", return_value=[self.issue(marker="Unreviewed issue")]), patch.object(audit, "run") as gh:
            with self.assertRaisesRegex(RuntimeError, r"filed after duplicate review \(#42\)"):
                audit.publish(self.directory, self.report)
        gh.assert_not_called()

    def test_reviewed_issue_state_change_does_not_block_publication(self):
        reviewed = {**self.issue(number=7, marker="Unrelated reviewed issue"), "state": "open"}
        audit.write_json(self.directory / "issues.json", [reviewed])
        self.report["snapshot_digest"] = audit.digest([reviewed])
        closed = {**reviewed, "state": "closed", "labels": ["needs triage", "wontfix"]}
        created = self.issue()
        created["labels"] = [{"name": "needs triage"}]
        with patch.object(audit, "inventory", return_value=[closed]), patch.object(audit, "ensure_label"), patch.object(
            audit, "run", side_effect=[created["url"], json.dumps(created)],
        ):
            result = audit.publish(self.directory, self.report)
        self.assertEqual(result["outcomes"][0]["status"], "created")

    def test_pull_request_churn_does_not_block_publication(self):
        pull = {**self.issue(number=9, marker="An unrelated pull request"), "is_pr": True}
        created = self.issue()
        created["labels"] = [{"name": "needs triage"}]
        with patch.object(audit, "inventory", return_value=[pull]), patch.object(audit, "ensure_label"), patch.object(
            audit, "run", side_effect=[created["url"], json.dumps(created)],
        ):
            result = audit.publish(self.directory, self.report)
        self.assertEqual(result["outcomes"][0]["status"], "created")

    def test_reconcile_clears_receipt_only_when_marker_is_absent_twice(self):
        receipts = self.directory / "receipts"
        receipts.mkdir()
        key = audit.fingerprint(self.candidate)
        audit.write_json(receipts / (key + ".json"), {"status": "pending", "marker": self.marker()})
        with patch.object(audit, "RECONCILE_SETTLE", 0), patch.object(audit, "inventory", return_value=[]):
            result = audit.reconcile(self.directory)
        self.assertEqual(result["reconciled"], [{"fingerprint": key, "status": "cleared"}])
        self.assertFalse((receipts / (key + ".json")).exists())

    def test_reconcile_adopts_an_issue_the_lost_response_created(self):
        receipts = self.directory / "receipts"
        receipts.mkdir()
        key = audit.fingerprint(self.candidate)
        receipt = receipts / (key + ".json")
        audit.write_json(receipt, {"status": "pending", "marker": self.marker()})
        with patch.object(audit, "RECONCILE_SETTLE", 0), patch.object(
            audit, "inventory", side_effect=[[], [self.issue()]],
        ):
            result = audit.reconcile(self.directory)
        self.assertEqual(result["reconciled"][0]["status"], "published")
        self.assertEqual(audit.read_json(receipt), {
            "status": "published", "number": 42,
            "url": "https://example.ghe.com/azure-core-cto/log-service/issues/42",
        })

    def test_republish_refuses_when_an_unreviewed_issue_was_filed_during_the_run(self):
        self.state["started"] = "2026-09-21T16:16:45+00:00"
        audit.write_json(self.directory / "run.json", self.state)
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        audit.write_json(folder / "resolution.json", {"verdict": "blocked"})
        audit.write_json(folder / "report.json", self.report)
        filed = {**self.issue(number=77, marker="Filed by a human mid-run"),
                 "created_at": "2026-09-21T17:00:00Z"}
        with patch.object(audit, "RECONCILE_SETTLE", 0), patch.object(
            audit, "inventory", return_value=[filed],
        ), patch.object(audit, "run") as gh:
            with self.assertRaisesRegex(RuntimeError, r"need duplicate review.*#77"):
                audit.republish(self.directory)
        gh.assert_not_called()
        self.assertFalse((self.lock / "owner.json").exists())

    def test_republish_proceeds_once_an_operator_records_the_review(self):
        self.state["started"] = "2026-09-21T16:16:45+00:00"
        audit.write_json(self.directory / "run.json", self.state)
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        audit.write_json(folder / "resolution.json", {"verdict": "blocked"})
        audit.write_json(folder / "report.json", self.report)
        filed = {**self.issue(number=77, marker="Filed by a human mid-run"),
                 "created_at": "2026-09-21T17:00:00Z"}
        created = self.issue()
        created["labels"] = [{"name": "needs triage"}]
        with patch.object(audit, "RECONCILE_SETTLE", 0), patch.object(
            audit, "inventory", return_value=[filed],
        ), patch.object(audit, "ensure_label"), patch.object(
            audit, "run", side_effect=[created["url"], json.dumps(created)],
        ):
            result = audit.republish(self.directory, reviewed=[77])
        self.assertEqual(result["reviewed"], [77])
        self.assertEqual(result["republished"][0]["outcomes"][0]["status"], "created")

    def test_republish_creates_the_issue_a_failed_publication_lost(self):
        self.state["started"] = "2026-09-21T16:16:45+00:00"
        audit.write_json(self.directory / "run.json", self.state)
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        audit.write_json(folder / "resolution.json", {"verdict": "blocked"})
        audit.write_json(folder / "report.json", self.report)
        created = self.issue()
        created["labels"] = [{"name": "needs triage"}]
        with patch.object(audit, "RECONCILE_SETTLE", 0), patch.object(
            audit, "inventory", return_value=[],
        ), patch.object(audit, "ensure_label"), patch.object(
            audit, "run", side_effect=[created["url"], json.dumps(created)],
        ):
            result = audit.republish(self.directory)
        self.assertEqual(result["republished"][0]["id"], "C001")
        self.assertEqual(result["republished"][0]["outcomes"][0]["status"], "created")
        self.assertEqual(audit.read_json(folder / "resolution.json")["outcomes"][0]["status"], "created")

    def test_republish_skips_candidates_that_were_never_approved(self):
        self.state["started"] = "2026-09-21T16:16:45+00:00"
        audit.write_json(self.directory / "run.json", self.state)
        folder = self.directory / "candidates" / "C001"
        folder.mkdir(parents=True)
        audit.write_json(folder / "resolution.json", {"verdict": "blocked"})
        rejected = {**self.report, "verification": {"decisions": [{**self.decision, "verdict": "rejected"}]}}
        audit.write_json(folder / "report.json", rejected)
        with patch.object(audit, "RECONCILE_SETTLE", 0), patch.object(
            audit, "inventory", return_value=[],
        ), patch.object(audit, "run") as gh:
            result = audit.republish(self.directory)
        self.assertEqual(result["republished"], [])
        gh.assert_not_called()

    def test_gh_read_retries_transient_faults_but_not_real_errors(self):
        with patch.object(audit, "run", side_effect=[RuntimeError("wsarecv: connection reset"), "ok"]) as gh:            self.assertEqual(audit.gh_read(["gh", "api", "x"], pause=0), "ok")
        self.assertEqual(gh.call_count, 2)
        with patch.object(audit, "run", side_effect=RuntimeError("Could not resolve to a Repository")) as gh:
            with self.assertRaisesRegex(RuntimeError, "Could not resolve"):
                audit.gh_read(["gh", "api", "x"], pause=0)
        self.assertEqual(gh.call_count, 1)

    def test_dry_run_writes_draft_without_labels_or_issues(self):
        self.state["options"]["publish"] = False
        audit.write_json(self.directory / "run.json", self.state)
        with patch.object(audit, "inventory", return_value=[]), patch.object(audit, "run") as gh:
            result = audit.publish(self.directory, self.report)
        self.assertEqual(result["outcomes"][0]["status"], "draft")
        self.assertTrue(Path(result["outcomes"][0]["path"]).is_file())
        gh.assert_not_called()

    def test_publish_uses_enterprise_target_and_verifies_label(self):
        created = self.issue()
        created["labels"] = [{"name": "needs triage"}]
        with patch.object(audit, "inventory", return_value=[]), patch.object(audit, "ensure_label") as label, patch.object(
            audit, "run", side_effect=[created["url"], json.dumps(created)],
        ) as gh:
            result = audit.publish(self.directory, self.report)
        self.assertEqual(result["outcomes"][0]["status"], "created")
        label.assert_called_once()
        create = gh.call_args_list[0].args[0]
        self.assertIn(self.state["target"], create)
        self.assertEqual(create[-2:], ["--label", "needs triage"])
        receipt = next((self.directory / "receipts").glob("*.json"))
        self.assertEqual(audit.read_json(receipt)["status"], "published")

    def test_ambiguous_create_is_never_blindly_retried(self):
        with patch.object(audit, "inventory", return_value=[]), patch.object(audit, "ensure_label"), patch.object(
            audit, "run", side_effect=RuntimeError("GitHub response lost"),
        ):
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                audit.publish(self.directory, self.report)
        with patch.object(audit, "inventory", return_value=[]), patch.object(audit, "run") as gh:
            with self.assertRaisesRegex(RuntimeError, "Uncertain previous"):
                audit.publish(self.directory, self.report)
        gh.assert_not_called()
        with patch.object(audit, "inventory", return_value=[self.issue()]), patch.object(audit, "run") as gh:
            result = audit.publish(self.directory, self.report)
        self.assertEqual(result["outcomes"][0]["status"], "recovered")
        gh.assert_not_called()

    def test_missing_label_after_create_leaves_pending_receipt(self):
        created = self.issue(labels=[])
        with patch.object(audit, "inventory", return_value=[]), patch.object(audit, "ensure_label"), patch.object(
            audit, "run", side_effect=[created["url"], json.dumps(created)],
        ):
            with self.assertRaisesRegex(RuntimeError, "marker/label"):
                audit.publish(self.directory, self.report)
        receipt = next((self.directory / "receipts").glob("*.json"))
        self.assertEqual(audit.read_json(receipt)["status"], "pending")

    def test_inventory_paginates_all_states_and_includes_prs(self):
        raw = {
            "number": 2, "title": "Fixed", "body": None, "state": "closed",
            "html_url": "https://example.ghe.com/pull/2", "labels": [],
            "pull_request": {},
        }
        with patch.object(audit, "run", return_value=json.dumps([[], [raw]])) as gh:
            entries = audit.inventory(self.state)
        self.assertTrue(entries[0]["is_pr"])
        argv = gh.call_args.args[0]
        self.assertIn("--paginate", argv)
        self.assertIn("--slurp", argv)
        self.assertIn("state=all", argv[-1])
        self.assertIn(self.state["host"], argv)

    def test_recorder_executes_command_and_preserves_nonzero_exit(self):
        with patch.object(audit, "run", side_effect=[self.state["sha"], "", ""]):
            result = audit.record(
                self.directory, "real-child", "repro", 10,
                [sys.executable, "-c", "print('assertion failed'); raise SystemExit(7)"],
            )
        self.assertEqual(result["exit_code"], 7)
        self.assertFalse(result["timed_out"])
        stdout = self.directory / "records" / "real-child" / "stdout.txt"
        self.assertIn("assertion failed", stdout.read_text())
        self.assertEqual(audit.file_digest(stdout), result["artifacts"]["stdout.txt"])

    def test_finish_retains_worktree_and_removes_only_owned_lock(self):
        self.make_plan(0)
        with patch.object(audit, "assert_clean"):
            self.assertTrue(audit.finish(self.directory)["completed"])
        self.assertTrue(self.worktree.is_dir())
        self.assertFalse(self.lock.exists())
        self.assertTrue((self.directory / "completed.json").is_file())

    def test_foreign_lock_is_not_released(self):
        audit.write_json(self.lock / "owner.json", {"run_dir": "another-run"})
        with self.assertRaisesRegex(ValueError, "does not own"):
            audit.finish(self.directory)
        self.assertTrue(self.lock.is_dir())

    def test_zero_findings_is_valid_and_does_not_publish(self):
        self.report["discovery"]["candidates"] = []
        self.report["reproduction"]["results"] = []
        self.report["verification"]["decisions"] = []
        with patch.object(audit, "inventory") as network:
            result = audit.publish(self.directory, self.report)
        self.assertEqual(result["outcomes"], [])
        network.assert_not_called()

    def test_recovery_reuses_report_without_deployment_or_network(self):
        plan = self.make_plan()
        with patch.object(audit, "run", return_value=self.state["sha"]) as command, patch.object(audit, "inventory") as network:
            result = audit.recover(self.directory)
        self.assertTrue(result["recovering"])
        self.assertEqual(result["saved_plan"], plan)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0], ["git", "rev-parse", "HEAD"])
        network.assert_not_called()

    def test_recovery_rejects_changed_source(self):
        self.make_plan()
        with patch.object(audit, "run", return_value="b" * 40):
            with self.assertRaisesRegex(ValueError, "HEAD changed"):
                audit.recover(self.directory)

    def make_plan(self, count=1):
        candidates = []
        sources = []
        mapping = []
        for index in range(1, count + 1):
            candidate = {**self.candidate, "id": f"C{index:03}", "invariant": f"invariant {index}"}
            candidates.append(candidate)
            sources.append({**candidate, "id": f"B{index:03}"})
            mapping.append({
                "source_id": f"B{index:03}", "candidate_id": candidate["id"],
                "disposition": "selected", "reason": "Distinct root cause.",
            })
        plan = {
            "preparation": self.report["preparation"],
            "discoveries": {
                "bug": {"coverage": "Wal", "candidates": sources},
                "dead_code": {"coverage": "No candidates", "candidates": []},
                "stability": {"coverage": "No candidates", "candidates": []},
                "performance": {"coverage": "No candidates", "candidates": []},
            },
            "discovery": {"coverage": "Wal", "candidates": candidates, "mapping": mapping},
        }
        with patch.object(audit, "assert_clean"):
            audit.queue(self.directory, plan)
        return plan

    def start(self, identifier="C001"):
        with patch.object(audit, "assert_clean"):
            return audit.start_candidate(self.directory, identifier)

    def decline(self, identifier="C001"):
        result = {**self.result, "id": identifier, "status": "rejected"}
        audit.handoff(self.directory, identifier, result)
        return audit.resolve_candidate(self.directory, identifier)

    def complete(self, identifier="C001"):
        with patch.object(audit, "assert_clean"):
            return audit.complete_candidate(self.directory, identifier, {"ready": True, "reason": "Restored."})

    def test_queue_preserves_all_candidates_and_requires_complete_discovery_mapping(self):
        plan = self.make_plan(2)
        self.assertEqual(len(audit.queue(self.directory, plan)["candidates"]), 2)
        plan["discovery"]["mapping"].pop()
        with self.assertRaisesRegex(ValueError, "dropped a discovery"):
            audit.validate_plan(self.state, plan)

    def test_consolidation_can_group_categories_or_defer_without_losing_sources(self):
        plan = self.make_plan()
        plan["discoveries"]["stability"]["candidates"] = [{**self.candidate, "id": "S001", "category": "stability"}]
        plan["discovery"]["mapping"].append({
            "source_id": "S001", "disposition": "selected", "candidate_id": "C001",
            "reason": "The same causal mechanism.",
        })
        audit.validate_plan(self.state, plan)
        plan["discovery"]["mapping"][-1].update(disposition="deferred", candidate_id="", reason="Budget.")
        audit.validate_plan(self.state, plan)
        plan["discovery"]["mapping"].append(copy.deepcopy(plan["discovery"]["mapping"][-1]))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            audit.validate_plan(self.state, plan)

    def test_candidate_cannot_start_until_previous_cleanup_passes(self):
        self.make_plan(2)
        self.start()
        self.decline()
        with self.assertRaisesRegex(ValueError, "Previous candidate"):
            self.start("C002")
        self.complete()
        self.assertEqual(self.start("C002")["candidate"]["id"], "C002")

    def test_rejected_candidate_skips_review_and_publication_but_requires_cleanup(self):
        self.make_plan()
        self.start()
        with patch.object(audit, "inventory") as network:
            outcome = self.decline()
        self.assertEqual(outcome["outcomes"], [])
        network.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "unfinished"):
            audit.finish(self.directory)
        self.complete()
        with patch.object(audit, "assert_clean"):
            result = audit.finish(self.directory)
        self.assertEqual(result["candidates"][0]["report"]["verification"]["decisions"][0]["verdict"], "rejected")

    def test_cleanup_failure_retains_ownership_and_blocks_next_candidate(self):
        self.make_plan(2)
        self.start()
        self.decline()
        with self.assertRaisesRegex(ValueError, "safe environment"):
            audit.complete_candidate(self.directory, "C001", {"ready": False, "reason": "Replica not recovered."})
        self.assertTrue((self.directory / "active.json").exists())
        with self.assertRaisesRegex(ValueError, "Previous candidate"):
            self.start("C002")

    def test_dirty_worktree_blocks_completion_even_when_agent_reports_ready(self):
        self.make_plan()
        self.start()
        self.decline()
        with patch.object(audit, "assert_clean", side_effect=ValueError("worktree is not clean")):
            with self.assertRaisesRegex(ValueError, "not clean"):
                audit.complete_candidate(self.directory, "C001", {"ready": True, "reason": "Claims restored."})
        self.assertFalse((self.directory / "candidates" / "C001" / "completed.json").exists())

    def test_failed_cluster_health_blocks_completion(self):
        self.make_plan()
        self.start()
        self.decline()
        folder = self.directory / "candidates" / "C001"
        audit.write_json(folder / "cluster-used.json", {"last_command": "failover"})
        with patch.object(audit, "assert_clean"), patch.object(
            audit, "record", return_value={"exit_code": 1, "timed_out": False},
        ) as recorder:
            with self.assertRaisesRegex(ValueError, "Cluster health check failed"):
                audit.complete_candidate(self.directory, "C001", {"ready": True, "reason": "Restored."})
        self.assertEqual(recorder.call_args.kwargs["scope"], "cluster")
        self.assertFalse((folder / "completed.json").exists())

    def test_recovery_filters_completed_candidates_and_does_not_replay_resolution(self):
        plan = self.make_plan(2)
        self.start()
        self.decline()
        self.complete()
        self.assertEqual([c["id"] for c in audit.queue(self.directory, plan)["candidates"]], ["C002"])
        self.start("C002")
        self.decline("C002")
        state = self.start("C002")
        self.assertTrue(state["has_result"])
        self.assertTrue(state["resolved"])
        with patch.object(audit, "publish") as publisher:
            audit.resolve_candidate(self.directory, "C002")
        publisher.assert_not_called()

    def test_inflight_command_blocks_recovery_and_completion(self):
        self.make_plan()
        self.start()
        self.decline()
        folder = self.directory / "candidates" / "C001"
        audit.write_json(folder / "inflight.json", {"pid": 123})
        with self.assertRaisesRegex(ValueError, "may still be running"):
            self.start()
        with self.assertRaisesRegex(ValueError, "unfinished"):
            self.complete()

    def test_handoff_rejects_another_candidate_or_changed_saved_result(self):
        self.make_plan()
        self.start()
        with self.assertRaisesRegex(ValueError, "active candidate"):
            audit.handoff(self.directory, "C001", {**self.result, "id": "C002"})
        audit.handoff(self.directory, "C001", self.result)
        with self.assertRaisesRegex(ValueError, "replace saved"):
            audit.handoff(self.directory, "C001", {**self.result, "actual": "Different claim"})

    def test_cross_candidate_evidence_is_rejected(self):
        state = {**self.state, "candidate_id": "C002"}
        with self.assertRaisesRegex(ValueError, "different candidate"):
            audit.check_evidence(state, {**self.candidate, "id": "C002"}, self.result)

    def test_local_recorder_tags_candidate_and_clears_inflight(self):
        self.make_plan()
        self.start()
        with patch.object(audit, "run", side_effect=[self.state["sha"], "", ""]):
            result = audit.record(self.directory, "C001-local", "control", 10, [
                sys.executable, "-c", "print('control passed')",
            ], scope="local")
        self.assertEqual(result["candidate_id"], "C001")
        folder = self.directory / "candidates" / "C001"
        self.assertFalse((folder / "inflight.json").exists())
        self.assertFalse((folder / "cluster-used.json").exists())

    def test_cluster_recorder_rejects_unready_baseline_before_launch(self):
        self.report["preparation"]["environment_ready"] = False
        self.make_plan()
        self.start()
        with patch.object(audit, "run", side_effect=[self.state["sha"], "", ""]), patch.object(subprocess, "Popen") as child:
            with self.assertRaisesRegex(ValueError, "ready, provenance"):
                audit.record(self.directory, "C001-live", "repro", 10, ["do-not-run"], scope="cluster")
        child.assert_not_called()

    def test_prior_draft_suppresses_same_cause_without_github(self):
        self.make_plan(2)
        self.start()
        audit.handoff(self.directory, "C001", self.result)
        candidate = audit.read_json(self.directory / "plan.json")["discovery"]["candidates"][0]
        first_report = copy.deepcopy(self.report)
        first_report["discovery"]["candidates"] = [candidate]
        folder = self.directory / "candidates" / "C001"
        audit.write_json(folder / "report.json", first_report)
        audit.write_json(folder / "resolution.json", {"outcomes": [{"status": "draft"}]})
        self.assertEqual(audit.previous_finding(self.directory, "C002", audit.fingerprint(candidate)), "C001")

    def test_empty_queue_finishes_without_candidate_agent_execution(self):
        plan = self.make_plan(0)
        self.assertEqual(audit.queue(self.directory, plan)["candidates"], [])
        with patch.object(audit, "assert_clean"):
            self.assertEqual(audit.finish(self.directory)["candidates"], [])

    def test_deleted_source_file_is_validated_against_pinned_commit(self):
        self.candidate["category"] = "dead_code"
        self.result["records"] = [
            self.evidence("before-delete", "baseline", 0),
            self.evidence("after-delete-build", "removed-build", 0, "-class Wal {}"),
            self.evidence("after-delete-test", "removed-test", 0, "-class Wal {}"),
        ]
        self.result["patch"] = "delete-file.patch"
        (self.directory / "delete-file.patch").write_text("-class Wal {}", encoding="utf-8")
        (self.worktree / "Wal.cs").unlink()
        with patch.object(audit, "run", return_value="blob") as git:
            self.assertEqual(len(self.validate()), 1)
        self.assertEqual(git.call_args.args[0], ["git", "cat-file", "-t", self.state["sha"] + ":Wal.cs"])

    def test_duplicate_verdict_still_reconciles_pending_receipt_and_label(self):
        receipts = self.directory / "receipts"
        receipts.mkdir()
        receipt = receipts / (audit.fingerprint(self.candidate) + ".json")
        audit.write_json(receipt, {"status": "pending"})
        entries = [self.issue()]
        audit.write_json(self.directory / "issues.json", entries)
        self.report["snapshot_digest"] = audit.digest(entries)
        self.decision.update(verdict="duplicate", duplicate_number=42)
        with patch.object(audit, "inventory", return_value=[self.issue(labels=[])]):
            with self.assertRaisesRegex(RuntimeError, "missing needs triage"):
                audit.publish(self.directory, self.report)
        self.assertEqual(audit.read_json(receipt)["status"], "pending")
        with patch.object(audit, "inventory", return_value=entries):
            result = audit.publish(self.directory, self.report)
        self.assertEqual(result["outcomes"][0]["status"], "recovered")
        self.assertEqual(audit.read_json(receipt)["status"], "published")

    def test_finish_retries_interruption_before_lock_release(self):
        self.make_plan(0)
        with patch.object(audit, "assert_clean"), patch.object(audit, "release_lock", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                audit.finish(self.directory)
        result = audit.finish(self.directory)
        self.assertTrue(result["completed"])
        self.assertFalse(self.lock.exists())
        self.assertEqual(audit.finish(self.directory), result)

    def test_finish_retries_after_lock_rename_or_owner_removal(self):
        for operation in ("unlink", "rmdir"):
            with self.subTest(operation=operation):
                audit.write_json(self.directory / "completed.json", {"result": {"completed": True}})
                if not self.lock.exists():
                    self.lock.mkdir()
                audit.write_json(self.lock / "owner.json", {"run_dir": str(self.directory)})
                original = getattr(Path, operation)

                def interrupt(path, *args, **kwargs):
                    if path.name.startswith("released-") or path.parent.name.startswith("released-"):
                        raise OSError("interrupted release")
                    return original(path, *args, **kwargs)

                with patch.object(Path, operation, interrupt):
                    with self.assertRaisesRegex(OSError, "interrupted release"):
                        audit.finish(self.directory)
                self.assertTrue(audit.finish(self.directory)["completed"])
                self.assertFalse(self.lock.exists())

    def test_completed_recovery_does_not_touch_another_runs_lock(self):
        self.make_plan(0)
        with patch.object(audit, "assert_clean"):
            result = audit.finish(self.directory)
        self.lock.mkdir()
        audit.write_json(self.lock / "owner.json", {"run_dir": "another-run"})
        recovered = audit.recover(self.directory)
        self.assertTrue(recovered["finalized"])
        self.assertEqual(recovered["final_result"], result)
        self.assertEqual(audit.read_json(self.lock / "owner.json")["run_dir"], "another-run")


if __name__ == "__main__":
    unittest.main()
