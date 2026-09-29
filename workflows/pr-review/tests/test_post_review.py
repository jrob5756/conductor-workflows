import contextlib
import base64
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import post_review
from review_format import recover, visible_body


DIFF = """diff --git a/code.py b/code.py
--- a/code.py
+++ b/code.py
@@ -1,1 +1,8 @@
 old
+one
+two
+three
+four
+five
+six
+seven
"""
OPEN = {"state": "open", "merged": False, "head": {"sha": "head"}}


def finding_payload(count=1):
    return {
        "mode": "findings",
        "opening": "Thanks for working on this. I found a few points to address before moving forward.",
        "approved": [
            {"id": f"b{i}", "body": f"Original {i}", "path": "code.py", "line": i,
             "source_type": "code"}
            for i in range(1, count + 1)
        ],
        "items": [{"finding_id": f"b{i}", "body": f"Written finding {i}"}
                  for i in range(1, count + 1)],
    }


class PublishingTests(unittest.TestCase):
    def execute(self, payload, states=None, posts=None, event="COMMENT", diff=DIFF):
        states = iter(states or [OPEN])
        posts = iter(posts or [(0, '{"html_url":"https://example/review"}', "")])
        calls = []

        def run(args, stdin=None):
            calls.append((args, stdin))
            if "--method" in args:
                return next(posts)
            self.assertEqual(args, ["gh", "api", "repos/owner/repo/pulls/1"])
            state = next(states)
            return state if isinstance(state, tuple) else (0, json.dumps(state), "")

        with patch.object(post_review, "run", side_effect=run), \
                patch.object(post_review, "load_diff", return_value=diff), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit):
                post_review.main(["owner/repo", "1", "head", "/tree", "main", "origin", event])
        return json.loads(stdout.getvalue()), calls

    def test_ten_findings_eight_inline_two_ci_body_counts(self):
        payload = finding_payload(10)
        for finding in payload["approved"][8:]:
            finding.update(path="", line=0, source_type="ci")
        result, calls = self.execute(payload)
        self.assertTrue(result["ok"])
        self.assertEqual((result["posted_count"], result["inline_posted"],
                          result["body_count"], result["inline_demoted"]), (10, 8, 2, 0))
        posted = json.loads(calls[-1][1])
        self.assertTrue(posted["body"].startswith(payload["opening"] + "\n\n10 approved findings"))
        self.assertEqual(posted["commit_id"], "head")
        self.assertEqual(len(posted["comments"]), 8)
        recovered = recover(posted["body"])
        self.assertEqual(len(recovered), 10)
        self.assertEqual([item["source_type"] for item in recovered[-2:]], ["ci", "ci"])
        self.assertEqual([item["body"] for item in recovered],
                         [item["body"] for item in payload["items"]])

    def test_thread_findings_reply_reopen_and_confirmations_resolve(self):
        payload = finding_payload(2)
        payload["approved"][0].update(title="Still open", thread_id="T1", comment_id=20,
                                      thread_resolved=True)
        payload["confirmations"] = [{"thread_id": "T2", "comment_id": 30, "body": "Fix confirmed.",
                                     "resolve": "resolve", "path": "old.py", "line": 4, "title": "Fixed"}]
        with patch.object(post_review, "apply_actions", return_value=(2, [])) as apply:
            result, calls = self.execute(payload)
        self.assertEqual((result["inline_posted"], result["body_count"],
                          result["thread_replies"], result["thread_confirmations"]), (1, 0, 1, 1))
        posted = json.loads(calls[-1][1])
        self.assertEqual(len(posted["comments"]), 1)
        self.assertIn("1 as replies on existing threads", posted["body"])
        self.assertIn("`old.py:4`: Fixed", posted["body"])
        self.assertEqual(len(recover(posted["body"])), 2)
        actions = apply.call_args.args[2]
        self.assertEqual([(a["thread_id"], a["resolve"]) for a in actions],
                         [("T1", "unresolve"), ("T2", "resolve")])
        self.assertEqual(actions[0]["body"], "Written finding 1")

    def test_thread_action_failures_are_reported_not_fatal(self):
        payload = finding_payload(1)
        payload["approved"][0].update(title="T", thread_id="T1", comment_id=20)
        with patch.object(post_review, "apply_actions", return_value=(0, ["boom"])):
            result, _ = self.execute(payload)
        self.assertTrue(result["ok"])
        self.assertEqual(result["thread_errors"], ["boom"])

    def test_malformed_missing_duplicate_unknown_items_never_reach_api(self):
        invalid_items = [
            [], None, {}, ["text"], [{}], [{"finding_id": "b1"}],
            [{"finding_id": "b1", "body": ""}],
            [{"finding_id": "b1", "body": ["not text"]}],
            [{"finding_id": "b1", "body": "a", "path": "evil.py"}],
            [{"finding_id": "b2", "body": "unknown"}],
            [{"finding_id": [], "body": "bad"}],
            [{"finding_id": "b1", "body": "a"}, {"finding_id": "b1", "body": "b"}],
        ]
        for items in invalid_items:
            with self.subTest(items=items):
                payload = finding_payload()
                payload["items"] = items
                payload["posted_count"] = 999
                result, calls = self.execute(payload)
                self.assertFalse(result["ok"])
                self.assertEqual(calls, [])

    def test_malformed_approved_findings_fail_before_post(self):
        for approved in (None, [], ["bad"], [{"id": "b1"}],
                         [{"id": "b1", "body": "x", "line": "3"}]):
            with self.subTest(approved=approved):
                payload = finding_payload()
                payload["approved"] = approved
                result, calls = self.execute(payload)
                self.assertFalse(result["ok"])
                self.assertEqual(calls, [])

    def test_fresh_head_and_state_required_for_every_mode(self):
        modes = [
            (finding_payload(), "COMMENT"),
            ({"mode": "concept", "body": "Concept feedback"}, "COMMENT"),
            ({"mode": "note", "body": "My note"}, "COMMENT"),
            ({"mode": "approval", "body": "LGTM"}, "APPROVE"),
        ]
        states = [
            {**OPEN, "head": {"sha": "new"}},
            {**OPEN, "state": "closed"},
            {**OPEN, "merged": True},
            {"head": {"sha": "head"}, "state": "open"},
            [], (1, "", "API unavailable"), (0, "not JSON", ""),
        ]
        for payload, event in modes:
            for state in states:
                with self.subTest(mode=payload["mode"], state=state):
                    result, calls = self.execute(payload, states=[state], event=event)
                    self.assertFalse(result["ok"])
                    self.assertEqual(len(calls), 1)
                    self.assertNotIn("--method", calls[0][0])

    def test_retry_refreshes_state_and_preserves_all_findings(self):
        payload = finding_payload(2)
        payload["approved"][1]["line"] = 999
        result, calls = self.execute(
            payload, states=[OPEN, OPEN],
            posts=[(1, "", "gh: Validation failed (HTTP 422)"),
                   (0, '{"html_url":"https://example/retry"}', "")],
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual([("--method" in args) for args, _ in calls], [False, True, False, True])
        self.assertEqual((result["body_count"], result["inline_demoted"]), (2, 2))
        retry = json.loads(calls[-1][1])
        self.assertNotIn("comments", retry)
        self.assertEqual(retry["commit_id"], "head")
        self.assertTrue(retry["body"].startswith(payload["opening"] + "\n\n"))
        self.assertEqual(retry["body"].count(payload["opening"]), 1)
        self.assertEqual([f["body"] for f in recover(retry["body"])],
                         [i["body"] for i in payload["items"]])

    def test_retry_stops_if_head_or_state_changes(self):
        for state in ({**OPEN, "head": {"sha": "new"}}, {**OPEN, "state": "closed"}):
            result, calls = self.execute(
                finding_payload(), states=[OPEN, state],
                posts=[(1, "", "gh: Validation failed (HTTP 422)")],
            )
            self.assertFalse(result["ok"])
            self.assertEqual(sum("--method" in args for args, _ in calls), 1)

    def test_ambiguous_post_failure_is_never_retried(self):
        for response in (
            (1, "", "timeout"), (1, "", "unexpected text mentioning 422"),
            (0, "not JSON", ""), (0, "{}", ""), (0, "null", ""),
        ):
            with self.subTest(response=response):
                result, calls = self.execute(finding_payload(), posts=[response])
                self.assertFalse(result["ok"])
                self.assertEqual(len(calls), 2)

    def test_unanchorable_valid_content_is_demoted_not_lost(self):
        result, calls = self.execute(finding_payload(), diff="")
        self.assertTrue(result["ok"])
        self.assertEqual((result["inline_posted"], result["body_count"],
                          result["inline_demoted"]), (0, 1, 1))
        self.assertEqual(recover(json.loads(calls[-1][1])["body"])[0]["body"],
                         "Written finding 1")

    def test_plain_modes_are_separate_and_note_is_verbatim(self):
        body = "  My note\n\nwith whitespace\n"
        for mode, event in (("note", "COMMENT"), ("concept", "COMMENT"), ("approval", "APPROVE")):
            result, calls = self.execute({"mode": mode, "body": body}, event=event)
            self.assertTrue(result["ok"])
            self.assertEqual(json.loads(calls[-1][1])["body"], body)
            self.assertEqual(result["posted_count"], 0)
        payload = {"mode": "note", "body": "note", "items": finding_payload()["items"]}
        result, calls = self.execute(payload)
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])

    def test_publishing_does_not_mutate_approved_input(self):
        payload = finding_payload()
        original = copy.deepcopy(payload)
        self.execute(payload)
        self.assertEqual(payload, original)

    def test_missing_or_invalid_opening_never_reaches_api(self):
        for opening in (None, "", " \n ", [], {}):
            with self.subTest(opening=opening):
                payload = finding_payload()
                payload["opening"] = opening
                result, calls = self.execute(payload)
                self.assertFalse(result["ok"])
                self.assertEqual(calls, [])
        payload = finding_payload()
        del payload["opening"]
        result, calls = self.execute(payload)
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])

    def test_opening_is_not_recovered_as_a_finding_and_edits_invalidate_provenance(self):
        payload = finding_payload()
        result, calls = self.execute(payload)
        self.assertTrue(result["ok"])
        body = json.loads(calls[-1][1])["body"]
        findings = recover(body)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["body"], "Written finding 1")
        self.assertIsNone(recover(body.replace(payload["opening"], "A new concern.")))
        encoded = base64.urlsafe_b64encode(json.dumps(findings).encode()).decode()
        legacy = visible_body(findings) + f"\n\n<!-- conductor-review:v1:{encoded} -->"
        self.assertEqual(recover(legacy), findings)

    def test_tailored_approval_body_is_posted_without_fixed_signoff(self):
        for body in (
            "Looks good. Thanks for fixing the timeout handling!",
            "LGTM, thanks for adding the export option.",
        ):
            with self.subTest(body=body):
                result, calls = self.execute({"mode": "approval", "body": body}, event="APPROVE")
                self.assertTrue(result["ok"])
                self.assertEqual(json.loads(calls[-1][1])["body"], body)


if __name__ == "__main__":
    unittest.main()
