import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge_readiness.py"
SPEC = importlib.util.spec_from_file_location("readiness_test_module", SCRIPT)
readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readiness)
SHA = "a" * 40


def pr(**changes):
    return {
        "headRefOid": SHA, "baseRefName": "main", "state": "OPEN", "isDraft": False,
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED",
        "url": "https://github.com/owner/repo/pull/1", **changes,
    }


def check(name="build", state="SUCCESS", bucket="pass", **changes):
    return {
        "name": name, "state": state, "bucket": bucket,
        "link": "https://github.com/owner/repo/actions/runs/1", "workflow": "CI", **changes,
    }


class ReadinessTests(unittest.TestCase):
    def inspect(self, snapshot=None, checks=None, after=SHA):
        with patch.object(readiness, "query", side_effect=[snapshot or pr(), {"headRefOid": after}]), patch.object(
            readiness, "inspect_checks", return_value=checks or {
                "issues": [], "pending": [], "no_checks_configured": False,
            },
        ):
            return readiness.inspect_readiness("owner/repo", "1", SHA)

    def test_ready(self):
        result = self.inspect()
        self.assertTrue(result["ok"])
        self.assertTrue(result["can_merge"])
        self.assertEqual(result["issues"], [])

    def test_each_ineligible_state_blocks(self):
        cases = [
            {"headRefOid": "b" * 40}, {"state": "CLOSED"}, {"state": "MERGED"},
            {"isDraft": True}, {"isDraft": None}, {"mergeable": "CONFLICTING"},
            {"mergeable": "UNKNOWN"}, {"mergeStateStatus": "BLOCKED"},
            {"mergeStateStatus": "BEHIND"}, {"mergeStateStatus": "UNKNOWN"},
            {"reviewDecision": "REVIEW_REQUIRED"}, {"reviewDecision": "CHANGES_REQUESTED"},
            {"reviewDecision": None},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.inspect(pr(**changes))
                self.assertTrue(result["ok"])
                self.assertFalse(result["can_merge"])
                self.assertTrue(result["issues"])

    def test_no_review_requirement_is_valid(self):
        self.assertTrue(self.inspect(pr(reviewDecision=""))["can_merge"])

    def test_head_changes_during_inspection(self):
        self.assertFalse(self.inspect(after="b" * 40)["can_merge"])

    def test_missing_failed_pending_checks_block(self):
        for key in ("issues", "pending"):
            with self.subTest(key=key):
                checks = {"issues": [], "pending": [], "no_checks_configured": False}
                checks[key] = ["Check failed or is missing — https://example.test/check"]
                result = self.inspect(checks=checks)
                self.assertFalse(result["can_merge"])
                self.assertIn("https://example.test/check", result["summary"])

    def test_proven_no_configured_checks_is_distinct(self):
        result = self.inspect(checks={"issues": [], "pending": [], "no_checks_configured": True})
        self.assertTrue(result["can_merge"])
        self.assertTrue(result["no_checks_configured"])
        self.assertIn("No required checks", result["summary"])

    def test_actual_no_checks_cli_diagnostic_uses_fetched_requirements(self):
        for contexts in ([], ["build"]):
            protection = {"data": {"repository": {"pullRequest": {"baseRef": {
                "branchProtectionRule": {
                    "requiresStatusChecks": bool(contexts), "requiredStatusCheckContexts": contexts,
                },
            }}}}}
            empty = subprocess.CompletedProcess([], 1, "", "no checks reported on the 'topic' branch\n")
            responses = [
                subprocess.CompletedProcess([], 0, json.dumps(pr()), ""),
                subprocess.CompletedProcess([], 0, json.dumps(protection), ""),
                subprocess.CompletedProcess([], 0, "[[]]", ""),
                empty, empty,
                subprocess.CompletedProcess([], 0, json.dumps({"headRefOid": SHA}), ""),
            ]
            with self.subTest(contexts=contexts), patch.object(readiness, "command", side_effect=responses) as command:
                result = readiness.inspect_readiness("owner/repo", "1", SHA)
            self.assertTrue(result["ok"])
            self.assertEqual(result["can_merge"], not contexts)
            self.assertEqual(result["no_checks_configured"], not contexts)
            if contexts:
                self.assertIn("Required check 'build' is missing", result["summary"])
            else:
                self.assertEqual(result["issues"], [])
            self.assertNotIn("--required", command.call_args_list[3].args[0])
            self.assertIn("--required", command.call_args_list[4].args[0])

    def test_query_failure_is_not_no_checks(self):
        with patch.object(readiness, "query", side_effect=readiness.QueryError("permission denied")):
            result = readiness.inspect_readiness("owner/repo", "1", SHA)
        self.assertFalse(result["ok"])
        self.assertFalse(result["can_merge"])
        self.assertIn("permission denied", result["error"])
        self.assertNotIn("no_checks_configured", result)

    def test_malformed_snapshot_is_failure(self):
        with patch.object(readiness, "query", return_value={"headRefOid": SHA}):
            self.assertFalse(readiness.inspect_readiness("owner/repo", "1", SHA)["ok"])

    def test_invalid_arguments_do_not_query(self):
        for args in (("-x/r", "1", SHA), ("owner/repo", "bad", SHA), ("owner/repo", "1", "short")):
            with self.subTest(args=args), patch.object(readiness, "query") as query:
                result = readiness.inspect_readiness(*args)
                self.assertFalse(result["ok"])
                query.assert_not_called()


class CheckTests(unittest.TestCase):
    def inspect(self, rows, required=(), configured=()):
        with patch.object(readiness, "required_contexts", return_value=set(configured)), patch.object(
            readiness, "check_rows", side_effect=[rows, list(required)],
        ):
            return readiness.inspect_checks("owner/repo", "1", "main")

    def test_status_categories_including_external_and_required(self):
        cases = [
            ("SUCCESS", "pass", None), ("SKIPPED", "skipping", None),
            ("NEUTRAL", "pass", None), ("FAILURE", "fail", "issues"),
            ("ERROR", "fail", "issues"), ("CANCELLED", "cancel", "issues"),
            ("TIMED_OUT", "fail", "issues"), ("ACTION_REQUIRED", "fail", "issues"),
            ("IN_PROGRESS", "pending", "pending"), ("PENDING", "pending", "pending"),
            ("EXPECTED", "pending", "pending"), ("NEW_UNKNOWN", "pass", "issues"),
        ]
        for state, bucket, target in cases:
            with self.subTest(state=state):
                row = check(state=state, bucket=bucket)
                result = self.inspect([row], [row], ["build"])
                if target:
                    self.assertEqual(len(result[target]), 1)
                    self.assertIn(row["link"], result[target][0])
                else:
                    self.assertFalse(result["issues"] or result["pending"])

    def test_missing_required_context_is_pending(self):
        result = self.inspect([], configured=["external/security"])
        self.assertIn("external/security", result["pending"][0])
        self.assertIn("missing", result["pending"][0])
        self.assertFalse(result["no_checks_configured"])

    def test_empty_reported_checks_requires_configuration_query(self):
        with patch.object(readiness, "required_contexts", side_effect=readiness.QueryError("403")):
            with self.assertRaises(readiness.QueryError):
                readiness.inspect_checks("owner/repo", "1", "main")

    def test_required_definitions_combine_classic_and_rulesets(self):
        protection = {"data": {"repository": {"pullRequest": {"baseRef": {
            "branchProtectionRule": {"requiresStatusChecks": True, "requiredStatusCheckContexts": ["build"]},
        }}}}}
        rules = [{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "security", "integration_id": 12}],
        }}]
        with patch.object(readiness, "query", return_value=protection), patch.object(
            readiness, "paginated", return_value=rules,
        ) as paginate:
            names = readiness.required_contexts("owner/repo", "1", "release/next")
        self.assertEqual(names, {"build", "security"})
        self.assertIn("release%2Fnext", paginate.call_args.args[0])

    def test_unprotected_branch_and_empty_rules_prove_no_configured_checks(self):
        data = {"data": {"repository": {"pullRequest": {"baseRef": {"branchProtectionRule": None}}}}}
        with patch.object(readiness, "query", return_value=data), patch.object(
            readiness, "paginated", return_value=[],
        ):
            self.assertEqual(readiness.required_contexts("owner/repo", "1", "main"), set())

    def test_malformed_configuration_is_not_empty(self):
        with patch.object(readiness, "query", return_value={"data": None}):
            with self.assertRaises(readiness.QueryError):
                readiness.required_contexts("owner/repo", "1", "main")

    def test_check_cli_exit_codes_preserve_failed_and_pending_data(self):
        for code in (0, 1, 8):
            with self.subTest(code=code), patch.object(
                readiness, "command", return_value=subprocess.CompletedProcess([], code, json.dumps([check()]), ""),
            ):
                self.assertEqual(len(readiness.check_rows("owner/repo", "1", False)), 1)

    def test_empty_cli_error_is_recognized_narrowly(self):
        for required in (False, True):
            for error, accepted in (
                ("no checks reported on the 'topic' branch", True),
                ("HTTP 403: no checks reported", False),
                ("no checks reported: permission denied", False),
                ("no checks reported on the 'topic' branch\nHTTP 403", False),
                ("no required checks reported on the 'topic' branch", required),
            ):
                with self.subTest(error=error, required=required), patch.object(
                    readiness, "command", return_value=subprocess.CompletedProcess([], 1, "", error),
                ):
                    if accepted:
                        self.assertEqual(readiness.check_rows("owner/repo", "1", required), [])
                    else:
                        with self.assertRaises(readiness.QueryError):
                            readiness.check_rows("owner/repo", "1", required)

    def test_malformed_check_json_fails(self):
        for text in (
            "not JSON", "{}", '[{"name":"build"}]',
            '[{"name":[],"state":"SUCCESS","bucket":"pass"}]',
            '[{"name":"build","state":"SUCCESS","bucket":[]}]',
        ):
            with self.subTest(text=text), patch.object(
                readiness, "command", return_value=subprocess.CompletedProcess([], 0, text, ""),
            ):
                with self.assertRaises(readiness.QueryError):
                    readiness.check_rows("owner/repo", "1", False)


class CommandTests(unittest.TestCase):
    def test_subprocess_errors_are_explicit(self):
        for error in (FileNotFoundError("gh"), subprocess.TimeoutExpired(["gh"], 30)):
            with self.subTest(error=error), patch.object(readiness.subprocess, "run", side_effect=error):
                with self.assertRaises(readiness.QueryError):
                    readiness.query(["pr", "view", "1"])

    def test_graphql_errors_are_explicit(self):
        with patch.object(readiness, "command", return_value=subprocess.CompletedProcess(
            [], 0, '{"data":null,"errors":[{"message":"denied"}]}', "",
        )):
            with self.assertRaises(readiness.QueryError):
                readiness.query(["api", "graphql"])

    def test_pagination_collects_all_pages(self):
        with patch.object(readiness, "query", return_value=[{"runs": [{"id": 1}]}, {"runs": [{"id": 2}]}]) as query:
            self.assertEqual(readiness.paginated("endpoint", "runs"), [{"id": 1}, {"id": 2}])
        self.assertIn("--paginate", query.call_args.args[0])
        self.assertIn("--slurp", query.call_args.args[0])

    def test_malformed_page_is_not_silently_ignored(self):
        for pages in ({}, [{"wrong": []}], [{"runs": [None]}]):
            with self.subTest(pages=pages), patch.object(readiness, "query", return_value=pages):
                with self.assertRaises(readiness.QueryError):
                    readiness.paginated("endpoint", "runs")

    def test_commands_are_bounded_and_read_only(self):
        with patch.object(readiness.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
            readiness.query(["pr", "view", "1"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertFalse(run.call_args.kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
