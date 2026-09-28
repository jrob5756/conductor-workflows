import json
from pathlib import Path
import subprocess
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CI_FINDING = {
    "severity": "BLOCKING", "title": "PR CI did not pass",
    "body": "CI failed: https://github.com/owner/repo/actions/runs/10",
    "suggestion": "Fix the failing job.",
}


class FollowupQuestionsTests(unittest.TestCase):
    def execute(self, script, payload):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / script)], input=json.dumps(payload),
            text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout)

    def build(self, **payload):
        return self.execute("build_followup_questions.py", payload)

    def test_ci_failure_survives_all_prior_points_addressed(self):
        result = self.build(
            items=[{"source_id": "p1", "status": "addressed", "title": "Prior bug"}],
            prior_items=[{"id": "p1", "body": "Fix prior bug"}],
            ci_findings=[CI_FINDING],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["addressed_count"], 1)
        self.assertEqual(result["ci_count"], 1)
        self.assertEqual(result["question_count"], 1)
        self.assertEqual(result["blocking_count"], 1)
        self.assertEqual(result["unsourced_count"], 0)
        finding = result["findings"][0]
        self.assertEqual(finding["status"], "ci")
        self.assertEqual(finding["body"], CI_FINDING["body"])
        self.assertNotIn("again", result["questions"][0]["hint"])
        self.assertNotIn("Still open", result["questions"][0]["text"])

    def test_ci_and_prior_findings_have_unique_ids_and_triage_independently(self):
        result = self.build(
            items=[{"source_id": "p1", "status": "not_addressed", "severity": "BLOCKING", "title": "Prior bug"}],
            prior_items=[{"id": "p1", "body": "Fix prior bug"}],
            ci_findings=[CI_FINDING],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["question_count"], 2)
        self.assertEqual([f["id"] for f in result["findings"]], ["b1", "b2"])
        applied = self.execute("apply_triage.py", {
            "findings": result["findings"],
            "items": [{"id": "b1", "answer": "Do not post"}, {"id": "b2", "answer": "Post as-is"}],
        })
        self.assertEqual(applied["approved_count"], 1)
        self.assertEqual(applied["approved"][0]["status"], "ci")
        self.assertEqual(applied["approved"][0]["body"], CI_FINDING["body"])
        self.assertEqual([q["id"] for q in result["questions"]], ["b1", "b2"])

    def test_passing_ci_does_not_add_findings(self):
        result = self.build(items=[], prior_items=[], ci_findings=[])
        self.assertTrue(result["ok"])
        self.assertEqual(result["question_count"], 0)
        self.assertEqual(result["ci_count"], 0)

    def test_ci_does_not_mask_unaccounted_prior_feedback(self):
        result = self.build(
            items=[], prior_items=[{"id": "p1", "body": "Fix prior bug", "source_type": "code"}],
            ci_findings=[CI_FINDING],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["unaccounted_count"], 1)
        self.assertEqual(result["question_count"], 2)
        prior = next(f for f in result["findings"] if f["status"] != "ci")
        self.assertEqual(prior["source_id"], "p1")
        self.assertEqual(prior["status"], "unclear")
        self.assertIn("Fix prior bug", prior["body"])

    def test_malformed_ci_fails_instead_of_dropping_findings(self):
        for findings in (None, {}, ["bad"], [{}], [{**CI_FINDING, "severity": "NIT"}],
                         [{**CI_FINDING, "body": None}], [{**CI_FINDING, "title": "", "body": ""}]):
            with self.subTest(findings=findings):
                result = self.build(items=[], prior_items=[], ci_findings=findings)
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])

    def test_legacy_payload_without_ci_is_supported(self):
        result = self.build(items=[{"status": "not_addressed", "title": "Prior bug"}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["question_count"], 1)
        self.assertEqual(result["ci_count"], 0)

    def test_three_addressed_points_do_not_reopen_duplicate_summary(self):
        prior = [{"id": "p1", "kind": "review", "body": "Fix A, B and C."}]
        prior += [{"id": f"p{i}", "body": f"Fix {title}."}
                  for i, title in enumerate(("A", "B", "C"), 2)]
        items = [{"source_ids": [f"p{i}"], "status": "addressed", "title": title}
                 for i, title in enumerate(("A", "B", "C"), 2)]
        result = self.build(
            prior_items=prior, items=items, ci_findings=[],
            duplicate_sources=[{"source_id": "p1", "covered_by": ["p2", "p3", "p4"],
                                "evidence": "The summary repeats A, B and C, with no other asks."}],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["addressed_count"], 3)
        self.assertEqual(result["question_count"], 0)
        self.assertEqual(result["duplicate_source_count"], 1)

    def test_multiple_sources_count_distinct_findings_not_comments(self):
        result = self.build(
            prior_items=[{"id": "p1", "body": "A and B"}, {"id": "p2", "body": "A"}],
            items=[
                {"source_ids": ["p1", "p2"], "status": "addressed", "title": "A"},
                {"source_ids": ["p1"], "status": "not_addressed", "title": "B"},
            ],
            ci_findings=[],
        )
        self.assertTrue(result["ok"])
        self.assertEqual((result["addressed_count"], result["question_count"]), (1, 1))
        self.assertEqual(result["findings"][0]["title"], "B")
        self.assertEqual(result["unaccounted_count"], 0)

    def test_unexamined_summary_and_body_only_findings_are_preserved(self):
        result = self.build(
            prior_items=[{"id": "p1", "body": "A plus another body-only concern"},
                         {"id": "p2", "body": "A"}],
            items=[{"source_ids": ["p2"], "status": "addressed", "title": "A"}],
            ci_findings=[],
        )
        self.assertFalse(result["ok"])
        self.assertIn("p1 is unexamined", result["error"])

    def test_unexamined_verified_body_only_code_reaches_triage(self):
        result = self.build(
            prior_items=[{"id": "p1", "body": "A body-only code concern", "source_type": "code"}],
            items=[], ci_findings=[],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["unaccounted_count"], 1)
        self.assertIn("body-only code concern", result["findings"][0]["body"])

    def test_prior_ci_uses_final_wait_even_if_model_says_still_running(self):
        for items in ([], [{"source_ids": ["p1"], "status": "not_addressed",
                           "title": "CI still running"}]):
            for ci_findings in ([], [CI_FINDING]):
                with self.subTest(items=items, ci_findings=ci_findings):
                    result = self.build(
                        prior_items=[{"id": "p1", "body": "CI still running", "source_type": "ci"},
                                     {"id": "p2", "body": "Real code bug", "source_type": "code"}],
                        items=items, ci_findings=ci_findings,
                    )
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["delegated_ci_count"], 1)
                    self.assertEqual(result["question_count"], 1 + len(ci_findings))
                    bodies = [finding["body"] for finding in result["findings"]]
                    self.assertTrue(any("Real code bug" in body for body in bodies))
                    self.assertFalse(any("CI still running" in body for body in bodies))

    def test_legacy_mixed_summary_splits_code_and_ci_before_delegation(self):
        result = self.build(
            prior_items=[{"id": "p1", "body": "CI pending. Also fix real code bug.",
                          "source_type": "legacy"}],
            items=[
                {"source_ids": ["p1"], "source_type": "ci", "status": "not_addressed",
                 "title": "CI pending"},
                {"source_ids": ["p1"], "source_type": "code", "status": "not_addressed",
                 "title": "Real code bug"},
            ],
            ci_findings=[],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["question_count"], 1)
        self.assertEqual(result["findings"][0]["title"], "Real code bug")

    def test_genuine_code_provenance_cannot_be_discarded_as_ci(self):
        result = self.build(
            prior_items=[{"id": "p1", "body": "Real code bug", "source_type": "code"}],
            items=[{"source_ids": ["p1"], "source_type": "ci",
                    "status": "not_addressed", "title": "Real code bug"}],
            ci_findings=[],
        )
        self.assertFalse(result["ok"])

    def test_duplicate_mapping_requires_examined_targets_and_evidence(self):
        for mapping in (
            None, ["bad"], [{"source_id": "p1", "covered_by": ["unknown"], "evidence": "x"}],
            [{"source_id": "p1", "covered_by": ["p2"], "evidence": ""}],
            [{"source_id": "p1", "covered_by": ["p1"], "evidence": "x"}],
            [{"source_id": "p1", "covered_by": ["p3"], "evidence": "x"}],
        ):
            with self.subTest(mapping=mapping):
                result = self.build(
                    prior_items=[{"id": "p1", "body": "Summary"}, {"id": "p2", "body": "A"},
                                 {"id": "p3", "body": "Not examined"}],
                    items=[{"source_ids": ["p2"], "status": "addressed", "title": "A"}],
                    duplicate_sources=mapping, ci_findings=[],
                )
                self.assertFalse(result["ok"])

    def test_malformed_source_ids_fail_closed(self):
        for sources in ("p1,p2", [None], [["p1"]], ["p1", "p1"]):
            result = self.build(
                items=[{"source_ids": sources, "title": "A", "status": "addressed"}],
                prior_items=[{"id": "p1", "body": "A"}], ci_findings=[],
            )
            self.assertFalse(result["ok"])

    def test_first_pass_provenance_is_assigned_by_pipeline_not_model(self):
        result = self.execute("build_questions.py", {
            "code_findings": [{**CI_FINDING, "source_type": "ci", "title": "Code bug"}],
            "ci_findings": [{**CI_FINDING, "source_type": "code"}],
        })
        self.assertTrue(result["ok"])
        self.assertEqual([item["source_type"] for item in result["findings"]], ["code", "ci"])
        applied = self.execute("apply_triage.py", {"findings": result["findings"], "items": []})
        self.assertEqual([item["source_type"] for item in applied["approved"]], ["code", "ci"])

    def test_triage_does_not_silently_drop_malformed_findings(self):
        finding = {"id": "b1", "body": "A real issue"}
        for findings in ([finding, "bad"], [finding, finding], [{"id": "b1", "body": ""}]):
            with self.subTest(findings=findings):
                applied = self.execute("apply_triage.py", {"findings": findings, "items": []})
                self.assertFalse(applied["ok"])
                self.assertEqual(applied["approved_count"], 0)


if __name__ == "__main__":
    unittest.main()
