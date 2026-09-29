import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prior_review.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("prior_review", SCRIPT)
prior_review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prior_review)
from review_format import render


class ChangesSinceReviewTests(unittest.TestCase):
    def test_identical_head_needs_no_git_call(self):
        with patch.object(prior_review, "run") as run:
            status, _ = prior_review.changes_since_review("abc", "abc", "/repo", True)
        self.assertEqual(status, "unchanged")
        run.assert_not_called()

    def test_tree_comparison(self):
        for exit_code, expected in ((0, "unchanged"), (1, "changed"), (128, "unknown")):
            with self.subTest(exit_code=exit_code):
                with patch.object(prior_review, "run", return_value=(exit_code, "", "error")) as run:
                    status, _ = prior_review.changes_since_review("old", "new", "/repo", True)
                self.assertEqual(status, expected)
                self.assertEqual(run.call_args.args[0][-4:], ["--quiet", "old", "new", "--"])

    def test_missing_baseline_is_not_unchanged(self):
        self.assertEqual(
            prior_review.changes_since_review("", "new", "/repo", True)[0], "unknown"
        )
        self.assertEqual(
            prior_review.changes_since_review("", "new", "/repo", False)[0],
            "no_previous_review",
        )

    def scan(self, reviews, inline=None, conversation=None, threads=None):
        with patch.object(
            prior_review, "fetch",
            side_effect=[(reviews, ""), (inline or [], ""), (conversation or [], "")],
        ), patch.object(
            prior_review, "fetch_threads", return_value=(threads or {}, "")
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                prior_review.main(["owner/repo", "1", "me", "other", "abc", "/repo"])
        return json.loads(output.getvalue())

    def test_empty_approval_from_other_login_still_gates_unchanged(self):
        result = self.scan([{
            "user": {"login": "OTHER"}, "state": "APPROVED", "body": "",
            "commit_id": "abc", "submitted_at": "2026-09-01T00:00:00Z",
        }])
        self.assertFalse(result["has_prior_review"])
        self.assertEqual(result["change_status"], "unchanged")

    def test_pending_review_does_not_establish_baseline(self):
        result = self.scan([{
            "id": 10, "user": {"login": "me"}, "state": "PENDING",
            "commit_id": "abc", "body": "Draft",
        }], [{"user": {"login": "me"}, "pull_request_review_id": 10, "body": "Draft"}])
        self.assertEqual(result["change_status"], "no_previous_review")

    def test_inline_review_uses_original_commit(self):
        result = self.scan([], [{
            "user": {"login": "me"}, "body": "Fix this",
            "original_commit_id": "abc", "commit_id": "later",
        }])
        self.assertEqual(result["change_status"], "unchanged")

    def test_conversation_only_is_unknown(self):
        result = self.scan([], conversation=[{"user": {"login": "me"}, "body": "Question"}])
        self.assertEqual(result["change_status"], "unknown")

    def test_structured_review_recovers_distinct_code_and_ci_not_summary(self):
        findings = [
            {"finding_id": "run:b1", "source_type": "code", "path": "a.py",
             "line": 3, "body": "Fix the bug", "placement": "inline"},
            {"finding_id": "run:b2", "source_type": "ci", "path": "",
             "line": 0, "body": "CI is still running", "placement": "body"},
            {"finding_id": "run:r1", "source_type": "code", "path": "",
             "line": 0, "body": "Add coverage for both operations", "placement": "body"},
        ]
        review = {"id": 10, "user": {"login": "me"}, "body": render(findings),
                  "state": "COMMENTED", "commit_id": "abc"}
        inline = {"id": 20, "user": {"login": "me"}, "pull_request_review_id": 10,
                  "path": "a.py", "original_line": 3, "body": "Fix the bug",
                  "created_at": "2026-01-01"}
        reply = {"id": 21, "user": {"login": "author"}, "in_reply_to_id": 20,
                 "body": "Fixed", "created_at": "2026-01-02"}
        result = self.scan([review], [inline, reply])
        self.assertEqual(len(result["prior_items"]), 3)
        self.assertEqual([item["source_type"] for item in result["prior_items"]],
                         ["code", "ci", "code"])
        self.assertEqual(result["prior_items"][0]["replies"][0]["body"], "Fixed")
        self.assertEqual([item["finding_id"] for item in result["prior_items"]],
                         [item["finding_id"] for item in findings])

    def test_inline_items_carry_thread_state_and_own_replies_are_not_items(self):
        thread = {"thread_id": "T1", "resolved": True, "root_comment_id": 20, "last_author": "author"}
        inline = [
            {"id": 20, "user": {"login": "me"}, "path": "a.py", "line": 3, "body": "Fix it",
             "created_at": "2026-01-01"},
            {"id": 21, "user": {"login": "me"}, "in_reply_to_id": 20, "body": "Still open",
             "created_at": "2026-01-02"},
            {"id": 22, "user": {"login": "author"}, "in_reply_to_id": 20, "body": "Done",
             "created_at": "2026-01-03"},
        ]
        result = self.scan([], inline, threads={20: thread, 21: thread, 22: thread})
        self.assertEqual(len(result["prior_items"]), 1)
        item = result["prior_items"][0]
        self.assertEqual((item["thread_id"], item["comment_id"]), ("T1", 20))
        self.assertTrue(item["thread_resolved"])
        self.assertTrue(item["awaiting_reply"])
        self.assertEqual(result["thread_error"], "")

    def test_thread_replies_from_earlier_review_are_not_new_items(self):
        findings = [{"finding_id": "run:b1", "source_type": "code", "path": "a.py", "line": 3,
                     "body": "Still needed", "placement": "thread", "title": "T"}]
        review = {"id": 10, "user": {"login": "me"}, "body": render(findings),
                  "state": "COMMENTED", "commit_id": "abc"}
        result = self.scan([review])
        self.assertEqual(result["prior_items"], [])

    def test_edited_summary_and_unrecognized_metadata_remain_reviewable(self):
        finding = {"finding_id": "run:b1", "source_type": "code", "path": "",
                   "line": 0, "body": "Body-only bug", "placement": "body"}
        for body in (
            "Extra unexamined concern\n" + render([finding]),
            "Review text\n\n<!-- conductor-review:v1:broken -->",
            "First issue. Second issue.",
        ):
            with self.subTest(body=body):
                result = self.scan([{"id": 10, "user": {"login": "me"},
                                     "body": body, "state": "COMMENTED"}])
                self.assertEqual(result["prior_items"][0]["body"], body)
                self.assertEqual(result["prior_items"][0]["source_type"], "legacy")

    def test_changed_inline_text_is_not_suppressed_by_manifest(self):
        finding = {"finding_id": "run:b1", "source_type": "code", "path": "a.py",
                   "line": 3, "body": "Original point", "placement": "inline"}
        result = self.scan(
            [{"id": 10, "user": {"login": "me"}, "body": render([finding]),
              "state": "COMMENTED"}],
            [{"id": 20, "user": {"login": "me"}, "pull_request_review_id": 10,
              "path": "a.py", "line": 3, "body": "Additional concern"}],
        )
        self.assertEqual(len(result["prior_items"]), 2)
        self.assertIn("Additional concern", [item["body"] for item in result["prior_items"]])


if __name__ == "__main__":
    unittest.main()
