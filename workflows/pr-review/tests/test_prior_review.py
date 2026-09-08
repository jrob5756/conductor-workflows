import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prior_review.py"
SPEC = importlib.util.spec_from_file_location("prior_review", SCRIPT)
prior_review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prior_review)


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

    def scan(self, reviews, inline=None, conversation=None):
        with patch.object(
            prior_review, "fetch",
            side_effect=[(reviews, ""), (inline or [], ""), (conversation or [], "")],
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


if __name__ == "__main__":
    unittest.main()
