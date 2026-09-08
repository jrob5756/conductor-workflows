import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("ci_test_module", SCRIPTS / "ci.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
SHA, MERGE = "a" * 40, "b" * 40
PR = {
    "head": {"sha": SHA, "ref": "topic", "repo": {"full_name": "owner/repo"}},
    "base": {"ref": "main"}, "state": "open", "merge_commit_sha": MERGE,
}
CHECKS = {"issues": [], "pending": [], "no_checks_configured": False}


def run(**changes):
    return {
        "id": 10, "workflow_id": 1, "run_attempt": 1, "head_sha": SHA,
        "status": "completed", "conclusion": "success", "event": "pull_request",
        "pull_requests": [{"number": 1, "head": {"sha": SHA}}],
        "head_branch": "topic", "head_repository": {"full_name": "owner/repo"},
        "html_url": "https://github.com/owner/repo/actions/runs/10", "name": "CI",
        **changes,
    }


def start_data(item=None, findings=None):
    item = item or ci.tracking(run(), "rerun")
    return ci.response(
        findings or [], runs=[item], run_ids=[item["id"]],
        nwo="owner/repo", pr_number="1", reviewed_head_sha=SHA,
    )


class DiscoveryTests(unittest.TestCase):
    def test_head_and_merge_sha_associations(self):
        with patch.object(ci, "paginated", side_effect=[
            [run(), run(id=20, workflow_id=2, pull_requests=[{"number": 2, "head": {"sha": SHA}}])],
            [run(id=30, workflow_id=3, head_sha=MERGE)],
        ]) as paginate:
            found = ci.discover("owner/repo", "1", SHA, PR)
        self.assertEqual([item["id"] for item in found], [10, 30])
        self.assertIn(f"head_sha={SHA}", paginate.call_args_list[0].args[0])
        self.assertIn(f"head_sha={MERGE}", paginate.call_args_list[1].args[0])

    def test_old_runs_and_attempts_are_not_restarted_twice(self):
        with patch.object(ci, "paginated", return_value=[
            run(id=9), run(id=10, run_attempt=1), run(id=10, run_attempt=3),
        ]):
            found = ci.discover("owner/repo", "1", SHA, {**PR, "merge_commit_sha": None})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["run_attempt"], 3)

    def test_fork_missing_association_is_verified_via_commit_prs(self):
        fork = {"full_name": "contributor/repo"}
        with patch.object(ci, "paginated", side_effect=[
            [run(pull_requests=[], head_repository=fork)], [{"number": 1, "head": {"sha": SHA}}], [],
        ]) as paginate:
            found = ci.discover("owner/repo", "1", SHA, {**PR, "head": {**PR["head"], "repo": fork}})
        self.assertEqual(len(found), 1)
        self.assertIn(f"commits/{SHA}/pulls", paginate.call_args_list[1].args[0])

    def test_fallback_test_merge_ref_is_pr_event_only(self):
        for event in ("pull_request", "push"):
            with self.subTest(event=event), patch.object(ci, "paginated", side_effect=[
                [], [run(head_sha=MERGE, head_branch="refs/pull/1/merge", event=event, pull_requests=[])],
                [{"number": 1, "head": {"sha": SHA}}],
            ]):
                if event == "pull_request":
                    self.assertEqual(len(ci.discover("owner/repo", "1", SHA, PR)), 1)
                else:
                    with self.assertRaises(ci.QueryError):
                        ci.discover("owner/repo", "1", SHA, PR)

    def test_latest_runs_are_selected_per_event_and_ref(self):
        with patch.object(ci, "paginated", return_value=[
            run(id=8, event="push"), run(id=9), run(id=10, event="push"), run(id=11),
            run(id=12, head_branch="refs/pull/1/merge"),
        ]):
            found = ci.discover("owner/repo", "1", SHA, {**PR, "merge_commit_sha": None})
        self.assertEqual([item["id"] for item in found], [10, 11, 12])

    def test_same_sha_without_correct_pr_association_is_not_enough(self):
        with patch.object(ci, "paginated", side_effect=[
            [run(pull_requests=[])], [{"number": 2, "head": {"sha": SHA}}], [],
        ]):
            self.assertEqual(ci.discover("owner/repo", "1", SHA, PR), [])

    def test_default_branch_and_privileged_workflows_are_not_substitutes(self):
        with patch.object(ci, "paginated", return_value=[
            run(event="pull_request_target"), run(event="workflow_dispatch"),
            run(head_sha="c" * 40), run(pull_requests=[{"number": 1, "head": {"sha": "d" * 40}}]),
        ]):
            self.assertEqual(ci.discover("owner/repo", "1", SHA, PR), [])

    def test_discovery_failure_and_malformed_runs_fail_closed(self):
        for value in (
            [run(run_attempt=None)], [run(status="unknown")], [run(status=[])],
            [run(conclusion=[])], [run(pull_requests=None)],
        ):
            with self.subTest(value=value), patch.object(ci, "paginated", return_value=value):
                with self.assertRaises(ci.QueryError):
                    ci.discover("owner/repo", "1", SHA, PR)


class StartTests(unittest.TestCase):
    def start(self, runs, command_result=None):
        with patch.object(ci, "read_pr", return_value=PR), patch.object(
            ci, "discover", return_value=runs,
        ), patch.object(ci, "command", return_value=command_result or subprocess.CompletedProcess([], 0, "", "")) as command:
            result = ci.start("owner/repo", "1", SHA)
        return result, command

    def test_completed_run_reruns_and_tracks_next_attempt(self):
        result, command = self.start([run(run_attempt=3)])
        self.assertFalse(result["findings"])
        self.assertEqual(result["runs"][0]["expected_attempt"], 4)
        self.assertIn("/rerun", command.call_args.args[0][1])
        self.assertEqual(command.call_args.args[0][-2:], ["--method", "POST"])

    def test_start_deadline_leaves_time_to_emit_blocking_feedback(self):
        with patch.object(ci.time, "monotonic", return_value=100), patch.object(
            ci, "read_pr", return_value=PR
        ) as read_pr, patch.object(
            ci, "discover", side_effect=ci.QueryError("CI verification timed out.")
        ) as discover:
            result = ci.start("owner/repo", "1", SHA)
        self.assertLess(ci.START_TIMEOUT, 300)
        self.assertEqual(read_pr.call_args.args[-1], 100 + ci.START_TIMEOUT)
        self.assertEqual(discover.call_args.args[-1], 100 + ci.START_TIMEOUT)
        self.assertTrue(result["ok"])
        self.assertEqual(result["findings"][0]["severity"], "BLOCKING")
        self.assertIn("timed out", result["summary"])

    def test_unverified_fallback_never_reruns_or_approves(self):
        for event in ("push", "pull_request"):
            for changes in (
                {"head_branch": "deploy"}, {"head_repository": {"full_name": "other/repo"}},
                {"head_branch": None}, {"head_repository": None},
            ):
                for conclusion in ("success", "action_required"):
                    candidate = run(event=event, conclusion=conclusion, pull_requests=[], **changes)
                    with self.subTest(event=event, changes=changes, conclusion=conclusion), patch.object(
                        ci, "read_pr", return_value=PR,
                    ), patch.object(ci, "paginated", side_effect=[
                        [candidate], [{"number": 1, "head": {"sha": SHA}}],
                    ]), patch.object(ci, "command") as command:
                        result = ci.start("owner/repo", "1", SHA)
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["run_ids"], [])
                    self.assertEqual(result["findings"][0]["severity"], "BLOCKING")
                    self.assertIn("repository/ref", result["summary"])
                    command.assert_not_called()

    def test_valid_push_and_pr_runs_are_both_started(self):
        with patch.object(ci, "read_pr", return_value={**PR, "merge_commit_sha": None}), patch.object(
            ci, "paginated", side_effect=[
                [run(event="push", pull_requests=[]), run(id=11)],
                [{"number": 1, "head": {"sha": SHA}}],
            ],
        ), patch.object(ci, "command", return_value=subprocess.CompletedProcess([], 0, "", "")) as command:
            result = ci.start("owner/repo", "1", SHA)
        self.assertFalse(result["findings"])
        self.assertEqual(result["run_ids"], [10, 11])
        self.assertEqual(command.call_count, 2)

    def test_external_contributor_approval_is_run_scoped(self):
        result, command = self.start([run(conclusion="action_required")])
        self.assertEqual(result["runs"][0]["expected_attempt"], 1)
        self.assertEqual(result["runs"][0]["action"], "approve")
        self.assertIn("/actions/runs/10/approve", command.call_args.args[0][1])
        self.assertNotIn("workflow_dispatch", str(command.call_args))

    def test_in_progress_run_is_not_rerun(self):
        result, command = self.start([run(status="in_progress", conclusion=None)])
        self.assertEqual(result["run_ids"], [10])
        command.assert_not_called()

    def test_empty_runs_are_blocking(self):
        result, command = self.start([])
        self.assertTrue(result["findings"])
        self.assertEqual(result["findings"][0]["severity"], "BLOCKING")
        command.assert_not_called()

    def test_approval_and_rerun_failure_are_explicit(self):
        for conclusion in ("success", "action_required"):
            with self.subTest(conclusion=conclusion):
                result, _ = self.start([run(conclusion=conclusion)], subprocess.CompletedProcess([], 403, "", "approval denied"))
                self.assertIn("approval denied", result["summary"])
                self.assertEqual(result["run_ids"], [])
                self.assertEqual(result["findings"][0]["severity"], "BLOCKING")

    def test_head_change_prevents_mutation(self):
        with patch.object(ci, "read_pr", side_effect=[PR, ci.QueryError("stale head"), ci.QueryError("stale head")]), patch.object(
            ci, "discover", return_value=[run()],
        ), patch.object(ci, "command") as command:
            result = ci.start("owner/repo", "1", SHA)
        self.assertIn("stale head", result["summary"])
        command.assert_not_called()

    def test_query_failure_is_not_empty_success(self):
        with patch.object(ci, "read_pr", side_effect=ci.QueryError("HTTP 403")):
            result = ci.start("owner/repo", "1", SHA)
        self.assertIn("403", result["summary"])

    def test_malformed_merge_sha_becomes_blocking(self):
        with patch.object(ci, "read_pr", return_value={**PR, "merge_commit_sha": ["bad"]}):
            with patch.object(ci, "paginated", return_value=[]):
                result = ci.start("owner/repo", "1", SHA)
        self.assertTrue(result["findings"])


class WaitTests(unittest.TestCase):
    def wait(self, current, data=None, checks=None, timeout=0.01):
        with patch.object(ci, "read_pr", return_value=PR), patch.object(
            ci, "discover", return_value=[],
        ), patch.object(ci, "query", return_value=current), patch.object(
            ci, "inspect_checks", return_value=checks or CHECKS,
        ), patch.object(ci, "failed_jobs", return_value="build: failure — https://example.test/job"), patch.object(
            ci.time, "sleep",
        ):
            return ci.wait("owner/repo", "1", SHA, data or start_data(), timeout=timeout)

    def test_rerun_cannot_accept_previous_success(self):
        result = self.wait(run(run_attempt=1))
        self.assertIn("timed out", result["summary"])
        self.assertIn("attempt 2", result["summary"])

    def test_new_attempt_succeeds(self):
        self.assertEqual(self.wait(run(run_attempt=2))["findings"], [])

    def test_approval_pending_waits_and_times_out_with_run_link(self):
        result = self.wait(run(conclusion="action_required"), start_data(ci.tracking(run(), "approve")))
        self.assertIn("approval", result["summary"])
        self.assertIn("https://github.com/owner/repo/actions/runs/10", result["summary"])
        self.assertIn("timed out", result["summary"])

    def test_each_terminal_failure_and_skip(self):
        for conclusion in ("failure", "cancelled", "timed_out", "action_required", "startup_failure", "unknown"):
            with self.subTest(conclusion=conclusion):
                result = self.wait(run(run_attempt=2, conclusion=conclusion))
                self.assertTrue(result["findings"])
                self.assertIn(conclusion, result["summary"])
                self.assertIn("https://example.test/job", result["summary"])
        for conclusion in ("skipped", "neutral"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(self.wait(run(run_attempt=2, conclusion=conclusion))["findings"], [])

    def test_startup_failures_are_preserved_after_success(self):
        original = ci.finding("Could not start secondary workflow", "permission denied")
        result = self.wait(run(run_attempt=2), start_data(findings=[original]))
        self.assertEqual(result["findings"], [original])
        self.assertNotIn("path", result["findings"][0])
        self.assertNotIn("line", result["findings"][0])

    def test_external_required_checks_and_missing_checks_are_waited_for(self):
        for status in ("Required security is missing.", "External scan is PENDING — https://example.test/scan"):
            with self.subTest(status=status):
                result = self.wait(run(run_attempt=2), checks={"issues": [], "pending": [status]})
                self.assertIn(status, result["summary"])
                self.assertIn("timed out", result["summary"])

    def test_failing_external_check_blocks(self):
        result = self.wait(run(run_attempt=2), checks={"issues": ["external: FAILURE"], "pending": []})
        self.assertIn("external: FAILURE", result["summary"])

    def test_head_change_during_wait_is_blocking(self):
        with patch.object(ci, "read_pr", side_effect=ci.QueryError("reviewed head is stale")):
            result = ci.wait("owner/repo", "1", SHA, start_data())
        self.assertIn("stale", result["summary"])

    def test_query_timeout_is_blocking(self):
        with patch.object(ci, "read_pr", side_effect=ci.QueryError("command timed out")):
            self.assertIn("timed out", ci.wait("owner/repo", "1", SHA, start_data())["summary"])

    def test_job_discovery_failure_is_explicit(self):
        with patch.object(ci, "read_pr", return_value=PR), patch.object(
            ci, "discover", return_value=[],
        ), patch.object(ci, "query", return_value=run(run_attempt=2, conclusion="failure")), patch.object(
            ci, "failed_jobs", side_effect=ci.QueryError("job query denied"),
        ):
            result = ci.wait("owner/repo", "1", SHA, start_data())
        self.assertIn("job query denied", result["summary"])

    def test_head_is_rechecked_after_results(self):
        with patch.object(ci, "read_pr", side_effect=[PR, ci.QueryError("head changed after checks")]), patch.object(
            ci, "discover", return_value=[],
        ), patch.object(ci, "query", return_value=run(run_attempt=2)), patch.object(
            ci, "inspect_checks", return_value=CHECKS,
        ):
            result = ci.wait("owner/repo", "1", SHA, start_data())
        self.assertIn("head changed after checks", result["summary"])

    def test_empty_tracking_cannot_pass(self):
        data = {**start_data(), "run_ids": [], "runs": []}
        result = self.wait(run(), data=data)
        self.assertIn("No PR CI runs", result["summary"])

    def test_empty_tracking_preserves_start_findings_and_error(self):
        original = ci.finding("No eligible workflows", "Cannot initiate correct PR CI.")
        data = {
            **start_data(findings=[original]), "run_ids": [], "runs": [],
            "error": "Discovery failed: permission denied",
        }
        result = self.wait(run(), data=data)
        self.assertTrue(result["ok"])
        self.assertIn(original, result["findings"])
        self.assertIn(data["error"], result["summary"])

    def test_start_failure_still_returns_readable_blocking_feedback(self):
        original = ci.finding("Could not approve fork CI", "Approval permission denied.")
        data = {
            **start_data(findings=[original]), "ok": False, "run_ids": [], "runs": [],
            "error": "Start could not complete",
        }
        result = self.wait(run(), data=data)
        self.assertTrue(result["ok"])
        self.assertIn(original, result["findings"])
        self.assertIn(data["error"], result["summary"])

    def test_wrong_target_or_missing_attempt_tracking_is_blocking(self):
        cases = [
            {**start_data(), "nwo": "other/repo"}, {**start_data(), "reviewed_head_sha": MERGE},
            {**start_data(), "runs": None}, {**start_data(), "runs": [{"id": 10}]},
            {**start_data(), "run_ids": [999]}, {**start_data(), "findings": None},
            {**start_data(), "ok": False, "error": "start failed"},
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assertTrue(self.wait(run(), data=data)["findings"])

    def test_new_workflow_discovered_at_wait_is_included(self):
        with patch.object(ci, "read_pr", return_value=PR), patch.object(
            ci, "discover", return_value=[run(id=20, workflow_id=2)],
        ), patch.object(ci, "query", side_effect=[run(run_attempt=2), run(id=20, workflow_id=2, conclusion="failure")]), patch.object(
            ci, "inspect_checks", return_value=CHECKS,
        ), patch.object(ci, "failed_jobs", return_value="job failed"):
            result = ci.wait("owner/repo", "1", SHA, start_data())
        self.assertIn("#20", result["summary"])

    def test_old_attempt_then_new_attempt(self):
        with patch.object(ci, "read_pr", return_value=PR), patch.object(
            ci, "discover", return_value=[],
        ), patch.object(ci, "query", side_effect=[run(), run(run_attempt=2)]), patch.object(
            ci, "inspect_checks", return_value=CHECKS,
        ), patch.object(ci.time, "sleep") as sleep:
            result = ci.wait("owner/repo", "1", SHA, start_data())
        self.assertEqual(result["findings"], [])
        sleep.assert_called_once_with(ci.POLL_INTERVAL)

    def test_cancelled_run_is_retired_only_for_verified_replacement(self):
        cancelled = run(run_attempt=2, conclusion="cancelled")
        replacement = run(id=11)
        with patch.object(ci, "read_pr", return_value={**PR, "merge_commit_sha": None}), patch.object(
            ci, "paginated", side_effect=[[run()], [cancelled, replacement]],
        ), patch.object(ci, "query", side_effect=[run(), replacement]) as query, patch.object(
            ci, "inspect_checks", return_value=CHECKS,
        ), patch.object(ci.time, "sleep") as sleep:
            result = ci.wait("owner/repo", "1", SHA, start_data())
        self.assertEqual(result["findings"], [])
        self.assertEqual([call.args[0][1].rsplit("/", 1)[-1] for call in query.call_args_list], ["10", "11"])
        sleep.assert_called_once_with(ci.POLL_INTERVAL)

    def test_replacement_does_not_retire_other_execution_identities(self):
        for changes in (
            {"event": "push"}, {"head_branch": "refs/pull/1/merge"},
            {"workflow_id": 2}, {"head_sha": MERGE},
        ):
            replacement = run(id=11, **changes)
            with self.subTest(changes=changes), patch.object(ci, "read_pr", return_value=PR), patch.object(
                ci, "discover", return_value=[replacement],
            ), patch.object(ci, "query", side_effect=[
                run(run_attempt=2, conclusion="cancelled"), replacement,
            ]), patch.object(ci, "inspect_checks", return_value=CHECKS), patch.object(
                ci, "failed_jobs", return_value="",
            ):
                result = ci.wait("owner/repo", "1", SHA, start_data())
            self.assertIn("#10", result["summary"])
            self.assertIn("cancelled", result["summary"])

    def test_pr_replacement_preserves_push_attempt_barrier(self):
        push = run(id=9, event="push")
        data = start_data()
        data["runs"].append(ci.tracking(push, "rerun"))
        data["run_ids"].append(9)
        with patch.object(ci, "read_pr", return_value={**PR, "merge_commit_sha": None}), patch.object(
            ci, "paginated", return_value=[push, run(conclusion="cancelled"), run(id=11)],
        ), patch.object(ci, "query", side_effect=[push, run(id=11)]), patch.object(
            ci, "inspect_checks", return_value=CHECKS,
        ), patch.object(ci.time, "sleep"):
            result = ci.wait("owner/repo", "1", SHA, data, timeout=0.01)
        self.assertIn("attempt 2", result["summary"])
        self.assertIn("#9", result["summary"])
        self.assertNotIn("cancelled", result["summary"])

    def test_cli_invalid_input_is_json_with_blocker(self):
        with patch.object(sys, "stdin", io.StringIO("not json")), contextlib.redirect_stdout(io.StringIO()) as out:
            ci.main(["wait", "owner/repo", "1", SHA])
        self.assertEqual(json.loads(out.getvalue())["findings"][0]["severity"], "BLOCKING")


if __name__ == "__main__":
    unittest.main()
