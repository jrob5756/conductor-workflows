import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import merge_pr


class MergeTests(unittest.TestCase):
    def invoke(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                merge_pr.main(argv)
        return json.loads(output.getvalue())

    def test_reviewed_head_required(self):
        with patch.object(merge_pr, "run") as run:
            result = self.invoke(["owner/repo", "1", "squash", "false", "true"])
        self.assertFalse(result["ok"])
        self.assertIn("reviewed head_sha", result["error"])
        run.assert_not_called()

    def test_readiness_rechecked_before_mutation(self):
        before = {"state": "OPEN", "url": "https://github.com/owner/repo/pull/1"}
        with patch.object(merge_pr, "read_state", return_value=(before, "")), patch.object(
            merge_pr, "inspect_readiness",
            return_value={"ok": True, "can_merge": False, "issues": ["Head changed"], "error": ""},
        ) as inspect, patch.object(merge_pr, "run") as run:
            result = self.invoke(["owner/repo", "1", "squash", "false", "true", "reviewed"])
        inspect.assert_called_once_with("owner/repo", "1", "reviewed")
        run.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("Head changed", result["error"])

    def test_merge_pins_reviewed_head(self):
        before = {"state": "OPEN", "headRefName": "feature", "url": "pr"}
        after = {"state": "MERGED", "mergedAt": "2026-09-08"}
        with patch.object(merge_pr, "read_state", side_effect=[(before, ""), (after, "")]), patch.object(
            merge_pr, "inspect_readiness",
            return_value={"ok": True, "can_merge": True, "issues": [], "error": ""},
        ), patch.object(merge_pr, "run", return_value=(0, "", "")) as run, patch.object(
            merge_pr, "branch_gone", return_value=True
        ):
            result = self.invoke(["owner/repo", "1", "squash", "false", "true", "reviewed"])
        self.assertTrue(result["merged"])
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--match-head-commit") + 1], "reviewed")
        self.assertNotIn("--auto", args)
        self.assertNotIn("--admin", args)

    def test_failed_merge_is_not_retried(self):
        before = {"state": "OPEN", "headRefName": "feature", "url": "pr"}
        with patch.object(merge_pr, "read_state", return_value=(before, "")), patch.object(
            merge_pr, "inspect_readiness",
            return_value={"ok": True, "can_merge": True, "issues": [], "error": ""},
        ), patch.object(merge_pr, "run", return_value=(1, "", "head mismatch")) as run:
            result = self.invoke(["owner/repo", "1", "squash", "false", "true", "reviewed"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "head mismatch")
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
