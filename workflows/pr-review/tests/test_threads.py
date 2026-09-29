import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import threads


class ThreadActionTests(unittest.TestCase):
    def test_reply_then_resolve_and_failures_do_not_stop_later_actions(self):
        calls = []

        def run(args, stdin=None):
            calls.append(args)
            if "comments/20/replies" in " ".join(args):
                return 1, "", "nope"
            return 0, json.dumps({"data": {}}), ""

        actions = [
            {"thread_id": "T1", "comment_id": 20, "body": "x", "resolve": "resolve"},
            {"thread_id": "T2", "comment_id": 30, "body": "Fix confirmed.", "resolve": "resolve"},
            {"thread_id": "T3", "comment_id": 40, "body": "Open", "resolve": "unresolve"},
        ]
        with patch.object(threads, "run", side_effect=run):
            done, errors = threads.apply_actions("o/r", "1", actions)
        self.assertEqual(done, 2)
        self.assertEqual(len(errors), 1)
        self.assertTrue(any("unresolveReviewThread" in " ".join(c) for c in calls))
        self.assertFalse(any("resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }" in " ".join(c)
                             and "T1" in " ".join(c) for c in calls))

    def test_fetch_threads_maps_every_comment_to_its_thread(self):
        page = {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"id": "T1", "isResolved": True,
                       "comments": {"nodes": [{"databaseId": 20}, {"databaseId": 21}]},
                       "latest": {"nodes": [{"author": {"login": "bob"}}]}}]}}}}}
        with patch.object(threads, "run", return_value=(0, json.dumps(page), "")):
            mapping, error = threads.fetch_threads("o/r", "1")
        self.assertEqual(error, "")
        self.assertEqual(mapping[21], {"thread_id": "T1", "resolved": True,
                                       "root_comment_id": 20, "last_author": "bob"})


if __name__ == "__main__":
    unittest.main()
