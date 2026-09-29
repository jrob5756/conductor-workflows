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
CHECKS = {
    "issues": [], "pending": [], "checks": [], "required_names": [],
    "no_checks_configured": True,
}


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

    def test_fork_with_both_associations_empty_matches_exact_head_ref(self):
        fork = {"full_name": "contributor/repo"}
        with patch.object(ci, "paginated", side_effect=[
            [run(pull_requests=[], head_repository=fork, conclusion="action_required")], [], [],
        ]):
            found = ci.discover("owner/repo", "1", SHA, {**PR, "head": {**PR["head"], "repo": fork}})
        self.assertEqual([item["id"] for item in found], [10])

    def test_empty_associations_require_exact_pr_execution_metadata(self):
        for changes in (
            {"head_branch": "other"}, {"head_repository": {"full_name": "other/repo"}},
            {"head_branch": None}, {"head_repository": None},
            {"head_sha": "c" * 40}, {"event": "push"},
        ):
            with self.subTest(changes=changes), patch.object(ci, "paginated", side_effect=[
                [run(pull_requests=[], **changes)], [], [],
            ]):
                self.assertEqual(ci.discover("owner/repo", "1", SHA, PR), [])

    def test_empty_associations_allow_only_exact_test_merge_ref(self):
        for branch in ("refs/pull/1/merge", "refs/pull/2/merge"):
            with self.subTest(branch=branch), patch.object(ci, "paginated", side_effect=[
                [], [run(head_sha=MERGE, head_branch=branch, pull_requests=[])], [],
            ]):
                found = ci.discover("owner/repo", "1", SHA, PR)
                self.assertEqual(len(found), int(branch == "refs/pull/1/merge"))

    def test_commit_association_query_error_is_not_an_empty_response(self):
        with patch.object(ci, "paginated", side_effect=[
            [run(pull_requests=[])], ci.QueryError("HTTP 403"),
        ]):
            with self.assertRaisesRegex(ci.QueryError, "403"):
                ci.discover("owner/repo", "1", SHA, PR)

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

    def test_passing_run_is_reused_without_rerun(self):
        for conclusion in sorted(ci.PASSING):
            with self.subTest(conclusion=conclusion):
                result, command = self.start([run(run_attempt=3, conclusion=conclusion)])
                self.assertFalse(result["findings"])
                self.assertEqual(result["runs"][0]["action"], "existing")
                self.assertEqual(result["runs"][0]["expected_attempt"], 3)
                command.assert_not_called()

    def test_completed_run_reruns_and_tracks_next_attempt(self):
        result, command = self.start([run(run_attempt=3, conclusion="failure")])
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
        self.assertFalse(result["ok"])
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
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["run_ids"], [])
                    self.assertEqual(result["findings"][0]["severity"], "BLOCKING")
                    self.assertIn("repository/ref", result["summary"])
                    command.assert_not_called()

    def test_valid_push_and_pr_runs_are_both_started(self):
        with patch.object(ci, "read_pr", return_value={**PR, "merge_commit_sha": None}), patch.object(
            ci, "paginated", side_effect=[
                [run(event="push", pull_requests=[], conclusion="failure"), run(id=11, conclusion="failure")],
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

    def test_fork_with_empty_associations_is_approved_then_rerun_on_next_pass(self):
        fork = {"full_name": "contributor/repo"}
        pr = {**PR, "head": {**PR["head"], "repo": fork}, "merge_commit_sha": None}
        for conclusion, action in (("action_required", "approve"), ("failure", "rerun")):
            with self.subTest(conclusion=conclusion), patch.object(
                ci, "read_pr", return_value=pr,
            ), patch.object(ci, "paginated", side_effect=[
                [run(pull_requests=[], head_repository=fork, conclusion=conclusion)], [],
            ]), patch.object(ci, "command", return_value=subprocess.CompletedProcess([], 0, "", "")) as command:
                result = ci.start("owner/repo", "1", SHA)
            self.assertFalse(result["findings"])
            self.assertEqual(result["runs"][0]["action"], action)
            self.assertEqual(result["runs"][0]["expected_attempt"], 1 if action == "approve" else 2)
            self.assertEqual(command.call_count, 1)
            self.assertIn(f"/actions/runs/10/{action}", command.call_args.args[0][1])

    def test_in_progress_run_is_not_rerun(self):
        result, command = self.start([run(status="in_progress", conclusion=None)])
        self.assertEqual(result["run_ids"], [10])
        command.assert_not_called()

    def test_empty_runs_are_provisional(self):
        result, command = self.start([])
        self.assertTrue(result["ok"])
        self.assertFalse(result["findings"])
        self.assertTrue(result["discovery_pending"])
        self.assertIn("provisional", result["summary"])
        command.assert_not_called()

    def test_approval_and_rerun_failure_are_explicit(self):
        for conclusion in ("failure", "action_required"):
            with self.subTest(conclusion=conclusion):
                result, _ = self.start([run(conclusion=conclusion)], subprocess.CompletedProcess([], 403, "", "approval denied"))
                self.assertIn("approval denied", result["summary"])
                self.assertEqual(result["run_ids"], [])
                self.assertEqual(result["findings"][0]["severity"], "BLOCKING")

    def test_authorization_refusal_stops_before_other_runs_or_review(self):
        for conclusion in ("failure", "action_required"):
            for error in (
                "gh: Must have admin rights to Repository. (HTTP 403)",
                "gh: Bad credentials (HTTP 401)",
                "Unauthorized: As an Enterprise Managed User, you cannot access this content",
            ):
                with self.subTest(conclusion=conclusion, error=error):
                    result, command = self.start([
                        run(conclusion=conclusion), run(id=11, conclusion=conclusion),
                    ], subprocess.CompletedProcess([], 1, "", error))
                    self.assertFalse(result["ok"])
                    self.assertTrue(result["auth_error"])
                    self.assertIn(error, result["error"])
                    self.assertEqual(command.call_count, 1)
                    self.assertFalse(result["discovery_pending"])

    def test_ci_server_error_is_not_classified_as_authentication(self):
        result, _ = self.start(
            [run(conclusion="failure")],
            subprocess.CompletedProcess([], 1, "", "GitHub unavailable (HTTP 503)"),
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["auth_error"])
        self.assertTrue(result["findings"])

    def test_approval_rejection_preserves_successfully_started_run_tracking(self):
        with patch.object(ci, "read_pr", return_value=PR), patch.object(
            ci, "discover", return_value=[run(conclusion="failure"), run(id=11, conclusion="failure")],
        ), patch.object(ci, "command", side_effect=[
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "HTTP 403"),
        ]):
            result = ci.start("owner/repo", "1", SHA)
        self.assertFalse(result["ok"])
        self.assertEqual(result["run_ids"], [10])

    def test_head_change_prevents_mutation(self):
        with patch.object(ci, "read_pr", side_effect=[PR, ci.QueryError("stale head"), ci.QueryError("stale head")]), patch.object(
            ci, "discover", return_value=[run(conclusion="failure")],
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

    def test_summary_explains_every_start_decision(self):
        result, command = self.start([
            run(id=10),
            run(id=11, status="in_progress", conclusion=None),
            run(id=12, conclusion="action_required"),
            run(id=13, conclusion="failure"),
        ])
        self.assertEqual(command.call_count, 2)
        self.assertEqual(
            [item["reason"] for item in result["runs"]],
            ["reused_passing", "already_running", "approved_fork", "retried_failed"],
        )
        for label, identifier in (
            ("Reused passing", 10), ("Already running", 11),
            ("Approved fork", 12), ("Retried failed", 13),
        ):
            self.assertRegex(result["summary"], rf"{label}: CI #{identifier}\b")
        self.assertFalse(result["discovery_pending"])

    def test_all_completed_nonpassing_conclusions_are_retried(self):
        for conclusion in ("failure", "cancelled", "timed_out", "startup_failure", "stale", "unknown"):
            with self.subTest(conclusion=conclusion):
                result, command = self.start([run(conclusion=conclusion)])
                self.assertEqual(result["runs"][0]["reason"], "retried_failed")
                self.assertEqual(result["runs"][0]["expected_attempt"], 2)
                self.assertIn("/rerun", command.call_args.args[0][1])

    def test_followup_full_review_inspects_again_before_reusing(self):
        with patch.object(ci, "read_pr", return_value=PR) as read_pr, patch.object(
            ci, "discover", side_effect=[[run()], [run(conclusion="failure")]],
        ) as discover, patch.object(
            ci, "command", return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as command:
            initial = ci.start("owner/repo", "1", SHA)
            followup = ci.start("owner/repo", "1", SHA)
        self.assertEqual(initial["runs"][0]["reason"], "reused_passing")
        self.assertEqual(followup["runs"][0]["reason"], "retried_failed")
        self.assertEqual(discover.call_count, 2)
        self.assertGreaterEqual(read_pr.call_count, 4)
        command.assert_called_once()

    def test_stale_head_before_and_after_mutation_is_structural(self):
        for responses, mutations in (
            ([ci.QueryError("stale head")], 0),
            ([PR, ci.QueryError("stale head")], 0),
            ([PR, PR, ci.QueryError("stale head")], 1),
        ):
            with self.subTest(mutations=mutations, responses=responses), patch.object(
                ci, "read_pr", side_effect=responses,
            ), patch.object(ci, "discover", return_value=[run(conclusion="failure")]), patch.object(
                ci, "command", return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as command:
                result = ci.start("owner/repo", "1", SHA)
            self.assertFalse(result["ok"])
            self.assertIn("stale head", result["error"])
            self.assertEqual(command.call_count, mutations)


class WaitTests(unittest.TestCase):
    def test_queued_cla_is_excluded_without_waiting_and_is_disclosed(self):
        from merge_readiness import inspect_checks

        module = sys.modules[inspect_checks.__module__]
        cla = {"name": "license/cla", "state": "QUEUED", "bucket": "pending",
               "workflow": "", "link": "https://github.com/apps/microsoft-github-policy-service"}
        for exclusions in (["license/cla"], []):
            with self.subTest(exclusions=exclusions), patch.object(ci, "read_pr", return_value=PR), \
                    patch.object(ci, "discover", return_value=[]), \
                    patch.object(ci, "query", return_value=run(run_attempt=2)), \
                    patch.object(module, "required_contexts", return_value={"license/cla"}), \
                    patch.object(module, "check_rows", return_value=[cla]), \
                    patch.object(ci.time, "sleep") as sleep:
                result = ci.wait("owner/repo", "1", SHA, start_data(), timeout=0.01,
                                 ignored_checks=exclusions)
            self.assertTrue(result["ok"])
            if exclusions:
                self.assertFalse(result["findings"])
                sleep.assert_not_called()
                self.assertIn("license/cla", result["summary"])
                self.assertIn("merge requirements still apply", result["summary"])
                self.assertIn("Non-excluded CI verified", result["summary"])
            else:
                self.assertIn("timed out", result["summary"])

    def test_excluded_check_does_not_suppress_underlying_actions_failure(self):
        with patch.object(ci, "read_pr", return_value=PR), \
                patch.object(ci, "discover", return_value=[]), \
                patch.object(ci, "query", return_value=run(run_attempt=2, conclusion="failure")), \
                patch.object(ci, "failed_jobs", return_value=[]), \
                patch.object(ci, "inspect_checks", return_value={**CHECKS, "ignored": ["license/cla: FAILURE"]}):
            result = ci.wait("owner/repo", "1", SHA, start_data(), ignored_checks=["license/cla"])
        self.assertTrue(result["findings"])
        self.assertIn("PR CI did not pass", result["summary"])
        self.assertIn("license/cla: FAILURE", result["summary"])

    def test_cli_accepts_explicit_exclusions_and_rejects_bad_shapes(self):
        with patch.object(ci, "wait", return_value=ci.response()) as wait, \
                patch.object(sys, "stdin", io.StringIO(json.dumps(start_data()))), \
                contextlib.redirect_stdout(io.StringIO()):
            ci.main(["wait", "owner/repo", "1", SHA, '["license/cla"]'])
        self.assertEqual(wait.call_args.kwargs["ignored_checks"], ["license/cla"])
        for raw in ('null', '"license/cla"', '[""]', 'not JSON'):
            with self.subTest(raw=raw), patch.object(ci, "wait") as wait, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                ci.main(["wait", "owner/repo", "1", SHA, raw])
            wait.assert_not_called()
            self.assertFalse(json.loads(out.getvalue())["ok"])

    def wait(self, current, data=None, checks=None, timeout=0.01):
        with patch.object(ci, "read_pr", return_value=PR), patch.object(
            ci, "discover", return_value=[],
        ), patch.object(ci, "query", return_value=current), patch.object(
            ci, "inspect_checks", return_value=checks or CHECKS,
        ), patch.object(ci, "failed_jobs", return_value=[
            {"name": "build", "conclusion": "failure", "html_url": "https://example.test/job"},
        ]), patch.object(
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

    def test_empty_tracking_rediscovery_query_error_is_fatal(self):
        data = {**start_data(), "run_ids": [], "runs": []}
        with patch.object(ci, "enabled_workflows", side_effect=ci.QueryError("Workflow query denied")):
            result = self.wait(run(), data=data)
        self.assertFalse(result["ok"])
        self.assertIn("Workflow query denied", result["error"])

    def test_empty_tracking_preserves_start_findings_and_error(self):
        original = ci.finding("No eligible workflows", "Cannot initiate correct PR CI.")
        data = {
            **start_data(findings=[original]), "run_ids": [], "runs": [],
            "error": "Discovery failed: permission denied",
        }
        result = self.wait(run(), data=data)
        self.assertFalse(result["ok"])
        self.assertIn(original, result["findings"])
        self.assertIn(data["error"], result["summary"])

    def test_start_failure_still_returns_readable_blocking_feedback(self):
        original = ci.finding("Could not approve fork CI", "Approval permission denied.")
        data = {
            **start_data(findings=[original]), "ok": False, "run_ids": [], "runs": [],
            "error": "Start could not complete",
        }
        result = self.wait(run(), data=data)
        self.assertFalse(result["ok"])
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
        ), patch.object(ci, "failed_jobs", return_value=[]):
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
                ci, "failed_jobs", return_value=[],
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


class FailureIdentityTests(unittest.TestCase):
    JOB_URL = "https://github.com/owner/repo/actions/runs/10/job/123"

    def wait(self, issues, jobs=None, job_error=None):
        with patch.object(ci, "read_pr", return_value=PR), patch.object(
            ci, "discover", return_value=[],
        ), patch.object(ci, "query", return_value=run(run_attempt=2, conclusion="failure")), patch.object(
            ci, "failed_jobs", return_value=jobs or [], side_effect=job_error,
        ), patch.object(ci, "inspect_checks", return_value={**CHECKS, "issues": issues}):
            return ci.wait("owner/repo", "1", SHA, start_data())

    def test_job_and_check_same_url_are_one_actionable_failure(self):
        result = self.wait(
            [f"CI / renamed check: FAILURE — {self.JOB_URL}"],
            [{"name": "build", "status": "completed", "conclusion": "failure", "html_url": self.JOB_URL}],
        )
        self.assertEqual(len(result["findings"]), 1)
        self.assertIn("build", result["summary"])
        self.assertIn(self.JOB_URL, result["summary"])

    def test_external_failure_with_same_title_is_not_lost(self):
        result = self.wait(
            [f"CI / build: FAILURE — {self.JOB_URL}", "CI / build: FAILURE — https://vendor.test/build"],
            [{"name": "build", "conclusion": "failure", "html_url": self.JOB_URL}],
        )
        self.assertEqual(len(result["findings"]), 2)
        self.assertIn("https://vendor.test/build", result["summary"])

    def test_unknown_identity_is_not_deduplicated_by_title(self):
        result = self.wait(
            ["CI / build: FAILURE"],
            [{"name": "build", "conclusion": "failure", "html_url": self.JOB_URL}],
        )
        self.assertEqual(len(result["findings"]), 2)

    def test_github_job_aliases_and_fragments_identify_same_job(self):
        for url in (
            self.JOB_URL + "?check_suite_focus=true#step:3:1",
            "https://github.com/owner/repo/runs/123?check_suite_focus=true",
        ):
            with self.subTest(url=url):
                result = self.wait(
                    [f"CI / build: FAILURE — {url}"],
                    [{"name": "build", "conclusion": "failure", "html_url": self.JOB_URL}],
                )
            self.assertEqual(len(result["findings"]), 1)

    def test_explicit_check_run_identity_deduplicates(self):
        result = self.wait(
            ["CI / build: FAILURE — https://github.com/owner/repo/checks?check_run_id=987"],
            [{
                "name": "build", "conclusion": "failure", "html_url": self.JOB_URL,
                "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/987",
            }],
        )
        self.assertEqual(len(result["findings"]), 1)

    def test_check_id_is_not_assumed_to_equal_job_id(self):
        result = self.wait(
            ["CI / build: FAILURE — https://github.com/owner/repo/checks?check_run_id=123"],
            [{"name": "build", "conclusion": "failure", "html_url": self.JOB_URL}],
        )
        self.assertEqual(len(result["findings"]), 2)

    def test_workflow_failure_without_failing_job_survives(self):
        result = self.wait([])
        self.assertEqual(len(result["findings"]), 1)
        self.assertIn("completed/failure", result["summary"])
        self.assertIn("/actions/runs/10", result["summary"])

    def test_workflow_link_deduplicates_but_not_unrelated_external_failure(self):
        result = self.wait([
            "CI: FAILURE — https://github.com/owner/repo/actions/runs/10",
            "CI: FAILURE — https://vendor.test/build",
        ])
        self.assertEqual(len(result["findings"]), 2)
        self.assertIn("https://vendor.test/build", result["summary"])

    def test_job_query_error_keeps_actionable_workflow_failure(self):
        result = self.wait([], job_error=ci.QueryError("job retrieval denied"))
        self.assertFalse(result["ok"])
        self.assertIn("job retrieval denied", result["error"])
        self.assertIn("completed/failure", result["summary"])
        self.assertIn("/actions/runs/10", result["summary"])

    def test_external_query_parameters_remain_distinct(self):
        result = self.wait([
            "External: FAILURE — https://vendor.test/build?job=1",
            "External: FAILURE — https://vendor.test/build?job=2",
        ])
        self.assertEqual(len(result["findings"]), 3)

    def test_external_checks_sharing_a_dashboard_link_remain_distinct(self):
        result = self.wait([
            "External: FAILURE — https://vendor.test/build",
            "Renamed external: FAILURE — https://vendor.test/build",
        ])
        self.assertEqual(len(result["findings"]), 3)

    def test_job_id_is_used_when_job_url_is_missing(self):
        result = self.wait(
            [f"CI / build: FAILURE — {self.JOB_URL}"],
            [{"id": 123, "name": "build", "conclusion": "failure"}],
        )
        self.assertEqual(len(result["findings"]), 1)

    def test_job_identity_is_repository_scoped(self):
        result = self.wait(
            ["CI: FAILURE — https://github.com/other/repo/actions/runs/10/job/123"],
            [{"conclusion": "failure", "html_url": self.JOB_URL}],
        )
        self.assertEqual(len(result["findings"]), 2)

    def test_failed_jobs_retains_structured_identity_and_only_nonpassing_jobs(self):
        failed = {"id": 123, "html_url": self.JOB_URL, "conclusion": "failure"}
        with patch.object(ci, "paginated", return_value=[
            failed, *({"conclusion": value} for value in ci.PASSING),
        ]) as paginate:
            jobs = ci.failed_jobs("owner/repo", run(run_attempt=3), 100)
        self.assertEqual(jobs, [failed])
        self.assertIn("/attempts/3/jobs", paginate.call_args.args[0])


class EmptyActionsTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        for name, value in (
            ("read_pr", PR), ("discover", []), ("query", run()),
            ("inspect_checks", CHECKS), ("enabled_workflows", []),
        ):
            mock = self.enterContext(patch.object(ci, name, return_value=value))
            setattr(self, name, mock)
        self.command = self.enterContext(patch.object(ci, "command", side_effect=AssertionError("Unexpected gh command")))
        self.enterContext(patch.object(ci.time, "monotonic", side_effect=lambda: self.now))
        self.sleep = self.enterContext(patch.object(ci.time, "sleep", side_effect=self.advance))
        self.data = {**start_data(), "runs": [], "run_ids": [], "discovery_pending": True}

    def advance(self, seconds):
        self.now += seconds

    def wait(self, **kwargs):
        return ci.wait("owner/repo", "1", SHA, self.data, **kwargs)

    def test_demonstrably_no_ci_returns_without_grace_delay(self):
        result = self.wait()
        self.assertTrue(result["ok"])
        self.assertEqual(result["findings"], [])
        self.assertTrue(result["no_ci"])
        self.assertIn("no enabled Actions", result["summary"])
        self.discover.assert_called_once()
        self.inspect_checks.assert_called_once()
        self.enabled_workflows.assert_called_once()
        self.assertEqual(self.read_pr.call_count, 2)
        self.sleep.assert_not_called()

    def test_empty_tracking_discovers_and_verifies_new_action(self):
        self.discover.return_value = [run()]
        result = self.wait()
        self.assertTrue(result["ok"])
        self.assertFalse(result["findings"])
        self.assertFalse(result["no_ci"])
        self.query.assert_called_once()
        self.enabled_workflows.assert_not_called()

    def test_late_run_arrives_during_discovery_grace(self):
        self.enabled_workflows.return_value = [{"state": "active"}]
        self.discover.side_effect = [[], [run()]]
        result = self.wait()
        self.assertFalse(result["findings"])
        self.assertEqual(self.discover.call_count, 2)
        self.assertEqual(self.inspect_checks.call_count, 2)
        self.sleep.assert_called_once_with(ci.POLL_INTERVAL)

    def test_enabled_workflows_without_runs_are_ambiguous_not_no_ci(self):
        self.enabled_workflows.return_value = [{"state": "active"}]
        result = self.wait()
        self.assertTrue(result["ok"])
        self.assertFalse(result["no_ci"])
        self.assertIn("applicability is unknown", result["summary"])
        self.assertEqual(self.now, ci.DISCOVERY_GRACE)

    def test_discovery_grace_is_bounded_by_overall_timeout(self):
        self.enabled_workflows.return_value = [{"state": "active"}]
        result = self.wait(timeout=2)
        self.assertIn("timed out", result["summary"])
        self.assertIn("Actions discovery", result["summary"])
        self.assertEqual(self.now, 2)

    def test_long_poll_interval_does_not_extend_discovery_grace(self):
        self.enabled_workflows.return_value = [{"state": "active"}]
        result = self.wait(poll_interval=ci.WAIT_TIMEOUT)
        self.assertIn("applicability is unknown", result["summary"])
        self.assertEqual(self.now, ci.DISCOVERY_GRACE)

    def test_external_passing_checks_without_actions_are_verified(self):
        self.inspect_checks.return_value = {
            **CHECKS, "checks": [{"name": "external", "state": "SUCCESS"}],
        }
        result = self.wait()
        self.assertTrue(result["ok"])
        self.assertFalse(result["findings"])
        self.assertFalse(result["no_ci"])
        self.sleep.assert_not_called()

    def test_external_failures_without_actions_remain_actionable(self):
        self.inspect_checks.return_value = {
            **CHECKS, "issues": ["external: FAILURE — https://vendor.test/build"],
        }
        result = self.wait()
        self.assertTrue(result["ok"])
        self.assertIn("https://vendor.test/build", result["summary"])
        self.assertFalse(result["no_ci"])

    def test_external_pending_checks_without_actions_are_polled(self):
        self.inspect_checks.side_effect = [
            {**CHECKS, "pending": ["external: PENDING"]},
            {**CHECKS, "checks": [{"name": "external", "state": "SUCCESS"}]},
        ]
        result = self.wait()
        self.assertFalse(result["findings"])
        self.assertEqual(self.discover.call_count, 2)
        self.sleep.assert_called_once_with(ci.POLL_INTERVAL)

    def test_missing_required_checks_without_actions_cannot_pass(self):
        self.inspect_checks.return_value = {
            **CHECKS, "pending": ["Required check 'security' is missing."],
            "required_names": ["security"], "no_checks_configured": False,
        }
        result = self.wait(timeout=2)
        self.assertIn("security", result["summary"])
        self.assertIn("timed out", result["summary"])

    def test_workflow_query_errors_fail_closed(self):
        self.enabled_workflows.side_effect = ci.QueryError("HTTP 403: workflows denied")
        result = self.wait()
        self.assertFalse(result["ok"])
        self.assertIn("workflows denied", result["error"])
        self.assertNotIn("No CI checks apply", result["summary"])

    def test_unknown_required_check_configuration_is_fatal(self):
        self.inspect_checks.side_effect = ci.QueryError("branch protection query denied")
        result = self.wait()
        self.assertFalse(result["ok"])
        self.assertIn("branch protection query denied", result["error"])

    def test_start_structural_error_is_preserved_without_queries(self):
        self.data.update(ok=False, error="Reviewed head is stale.")
        result = self.wait()
        self.assertFalse(result["ok"])
        self.assertEqual("Reviewed head is stale.", result["error"])
        self.read_pr.assert_not_called()
        self.discover.assert_not_called()
        self.inspect_checks.assert_not_called()

    def test_start_nonempty_error_cannot_be_dropped_even_if_ok_true(self):
        self.data.update(error="Discovery failed.")
        result = self.wait()
        self.assertFalse(result["ok"])
        self.assertIn("Discovery failed", result["error"])
        self.discover.assert_not_called()

    def test_start_structural_diagnostic_is_not_duplicated(self):
        self.data.update(ci.fatal("Reviewed head is stale."))
        result = self.wait()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], self.data["error"])
        self.assertEqual(result["findings"], self.data["findings"])

    def test_final_head_check_is_structural_even_without_actions(self):
        self.read_pr.side_effect = [PR, ci.QueryError("Reviewed head is stale.")]
        result = self.wait()
        self.assertFalse(result["ok"])
        self.assertIn("Reviewed head is stale", result["error"])


class ContractTests(unittest.TestCase):
    def test_finding_and_response_include_ci_provenance(self):
        finding = ci.finding("CI failed", "A job failed.")
        self.assertEqual(finding["source_type"], "ci")
        legacy = {key: value for key, value in finding.items() if key != "source_type"}
        for result in (ci.response([legacy]), ci.fatal("query denied", [legacy])):
            self.assertTrue(all(item["source_type"] == "ci" for item in result["findings"]))
        self.assertNotIn("source_type", legacy)

    def test_wait_normalizes_preserved_legacy_start_findings(self):
        legacy = {
            "severity": "BLOCKING", "title": "Approval denied",
            "body": "Permission denied.", "suggestion": "Request approval.",
        }
        data = {**start_data(findings=[legacy]), "ok": False, "error": "Stale head."}
        data["findings"] = [legacy]
        result = ci.wait("owner/repo", "1", SHA, data)
        self.assertFalse(result["ok"])
        self.assertEqual(result["findings"][0], {**legacy, "source_type": "ci"})
        self.assertTrue(all(item["source_type"] == "ci" for item in result["findings"]))

    def test_cli_malformed_input_always_returns_structural_contract(self):
        for argv, stdin in (
            ([], ""), (["bad", "owner/repo", "1", SHA], ""),
            (["start", "owner/repo", "0", SHA], ""),
            (["start", "owner/repo", "1", "short"], ""),
            (["wait", "owner/repo", "1", SHA], "bad json"),
            (["wait", "owner/repo", "1", SHA], "null"),
            (["wait", "owner/repo", "1", SHA], "[]"),
            (["wait", "owner/repo", "1", SHA], "{}"),
        ):
            with self.subTest(argv=argv, stdin=stdin), patch.object(
                sys, "stdin", io.StringIO(stdin),
            ), contextlib.redirect_stdout(io.StringIO()) as out, patch.object(
                ci, "command", side_effect=AssertionError("Malformed input must not query GitHub"),
            ):
                ci.main(argv)
            payload = json.loads(out.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIsInstance(payload["error"], str)
            self.assertTrue(payload["error"])
            self.assertIsInstance(payload["summary"], str)
            self.assertTrue(payload["summary"])
            self.assertIsInstance(payload["findings"], list)
            self.assertTrue(all(item["source_type"] == "ci" for item in payload["findings"]))

    def test_actual_read_pr_rejects_stale_head(self):
        with patch.object(ci, "query", return_value={**PR, "head": {"sha": MERGE}}):
            result = ci.start("owner/repo", "1", SHA)
        self.assertFalse(result["ok"])
        self.assertIn("Reviewed head is stale", result["error"])

    def test_enabled_workflows_requires_verified_configuration(self):
        with patch.object(ci, "paginated", return_value=[
            {"state": "disabled_fork"}, {"state": "disabled_inactivity"},
            {"state": "disabled_manually"}, {"state": "deleted"}, {"state": "active"},
        ]):
            self.assertEqual(ci.enabled_workflows("owner/repo", 100), [{"state": "active"}])
        for value in ({}, {"state": "new_unknown"}, {"state": []}):
            with self.subTest(value=value), patch.object(ci, "paginated", return_value=[value]):
                with self.assertRaises(ci.QueryError):
                    ci.enabled_workflows("owner/repo", 100)


if __name__ == "__main__":
    unittest.main()
