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
            items=[], prior_items=[{"id": "p1", "body": "Fix prior bug"}],
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


if __name__ == "__main__":
    unittest.main()
