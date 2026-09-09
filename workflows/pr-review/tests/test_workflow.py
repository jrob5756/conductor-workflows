from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "workflow.yaml"


class WorkflowRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = yaml.safe_load(WORKFLOW.read_text())
        cls.agents = {agent["name"]: agent for agent in cls.document["agents"]}
        cls.jinja = Environment(undefined=StrictUndefined)

    def route(self, name, output, **context):
        for route in self.agents[name]["routes"]:
            condition = route.get("when")
            if condition is None or self.jinja.from_string(condition).render(
                output=output, **context
            ).strip() == "True":
                return route["to"]
        self.fail(f"No matching route for {name}")

    def test_preflight_gates_unchanged_and_unknown(self):
        for status in ("unchanged", "unknown"):
            self.assertEqual(
                self.route("prior_review", {"ok": True, "change_status": status}),
                "unchanged_gate",
            )
        self.assertEqual(
            self.route("prior_review", {"ok": True, "change_status": "changed"}),
            "select_review",
        )
        self.assertEqual(self.route("prior_review", {"ok": False}), "cleanup")
        options = self.agents["unchanged_gate"]["options"]
        self.assertEqual([option["route"] for option in options], ["select_review", "cleanup"])
        self.assertEqual(self.route("select_review", {"followup": True}), "ci_start")
        self.assertEqual(self.route("select_review", {"followup": False}), "concept_review")

    def test_code_review_always_bracketed_by_ci(self):
        self.assertEqual(self.route("concept_review", {"blocking": [], "verdict": "good_addition"}), "ci_start")
        self.assertEqual(self.agents["concept_gate"]["options"][0]["route"], "ci_start")
        self.assertEqual(self.route("ci_start", {"ok": False}, concept_review={"output": {}}), "code_review")
        self.assertEqual(self.route("code_review", {"findings": []}), "ci_wait")
        self.assertEqual(self.route("code_review", {"findings": [{"title": "bug"}]}), "ci_wait")
        self.assertEqual(self.route("ci_wait", {"ok": True}, code_review={"output": {}}), "build_questions")
        self.assertEqual(self.route("ci_wait", {"ok": False}), "cleanup")

    def test_followup_always_starts_and_waits_for_ci(self):
        self.assertEqual(self.route("select_review", {"followup": True}), "ci_start")
        for ok in (True, False):
            self.assertEqual(self.route("ci_start", {"ok": ok}), "followup_review")
        self.assertEqual(self.route("followup_review", {"items": []}), "ci_wait")
        self.assertEqual(self.route("ci_wait", {"ok": True}), "build_followup_questions")
        self.assertEqual(self.route("ci_wait", {"ok": False}), "cleanup")
        self.assertIn("concept_review.output.verdict?", self.agents["ci_start"]["input"])
        self.assertIn("code_review.output.findings?", self.agents["ci_wait"]["input"])

    def test_followup_escalation_runs_ci_again_on_full_review_path(self):
        option = next(option for option in self.agents["followup_clear_gate"]["options"]
                      if option["value"] == "full_review")
        self.assertEqual(option["route"], "concept_review")
        context = {"followup_review": {"output": {}}, "concept_review": {"output": {}}}
        self.assertEqual(
            self.route("concept_review", {"blocking": [], "verdict": "good_addition"}, **context),
            "ci_start",
        )
        self.assertEqual(self.route("ci_start", {"ok": True}, **context), "code_review")
        self.assertEqual(self.route("code_review", {"findings": []}, **context), "ci_wait")
        self.assertEqual(
            self.route("ci_wait", {"ok": True}, code_review={"output": {}}, **context),
            "build_questions",
        )

    def test_followup_ci_findings_bypass_model_and_prevent_clear_route(self):
        import json

        findings = [{"severity": "BLOCKING", "title": "CI failed", "body": "Build failed", "suggestion": "Fix build"}]
        rendered = self.jinja.from_string(self.agents["build_followup_questions"]["stdin"]).render(
            followup_review={"output": {"items": []}},
            prior_review={"output": {"prior_items": []}},
            ci_wait={"output": {"findings": findings}},
        )
        self.assertEqual(json.loads(rendered)["ci_findings"], findings)
        self.assertEqual(
            self.route("build_followup_questions", {"ok": True, "question_count": 1}),
            "followup_triage",
        )
        self.assertEqual(
            self.route("build_followup_questions", {"ok": True, "question_count": 0}),
            "followup_clear_gate",
        )
        for name in ("followup_gate", "followup_clear_gate"):
            self.assertIn("ci_wait.output.summary", self.agents[name]["input"])
            self.assertIn("{{ ci_wait.output.summary }}", self.agents[name]["prompt"])

    def test_ci_findings_are_included_without_model_filtering(self):
        import json

        rendered = self.jinja.from_string(self.agents["build_questions"]["stdin"]).render(
            code_review={"output": {"findings": []}},
            ci_wait={"output": {"findings": [{"severity": "BLOCKING", "title": "CI failed"}]}},
        )
        self.assertEqual(json.loads(rendered), [{"severity": "BLOCKING", "title": "CI failed"}])

    def test_clean_and_dropped_findings_offer_approval(self):
        self.assertEqual(self.route("build_questions", {"ok": True, "question_count": 0}), "review_clear_gate")
        self.assertEqual(self.route("apply_triage", {"ok": True, "approved_count": 0}), "review_clear_gate")
        self.assertEqual(self.route("apply_triage", {"ok": False}), "cleanup")
        self.assertEqual(self.agents["review_clear_gate"]["options"][0]["route"], "post_approval")

    def test_approval_and_merge_are_separate(self):
        self.assertEqual(self.route("post_approval", {"ok": True}), "merge_readiness")
        self.assertEqual(self.route("post_approval", {"ok": False}), "cleanup")
        self.assertEqual(self.route("merge_readiness", {"ok": True, "can_merge": True}), "merge_gate")
        self.assertEqual(self.route("merge_readiness", {"ok": True, "can_merge": False}), "merge_blocked_gate")
        self.assertEqual(self.route("merge_readiness", {"ok": False}), "merge_blocked_gate")
        merge_sources = [
            agent["name"] for agent in self.agents.values()
            if any(route.get("to", route.get("route")) == "merge_pr"
                   for route in agent.get("routes", []) + agent.get("options", []))
        ]
        self.assertEqual(merge_sources, ["merge_gate"])
        self.assertIn("{{ pr_worktree.output.head_sha }}", self.agents["merge_pr"]["args"])

    def test_preflight_inputs_are_available_at_execution(self):
        self.assertNotIn("pr_worktree.output.head_sha", self.agents["author_gate"]["input"])
        self.assertIn("pr_worktree.output.head_sha", self.agents["prior_review"]["input"])
        self.assertIn("pr_worktree.output.worktree_path", self.agents["prior_review"]["input"])

    def test_concept_pass_does_not_load_broader_review_skill(self):
        concept = self.agents["concept_review"]
        self.assertEqual(concept["plugins"], [])
        self.assertNotIn("/concept-review", concept["prompt"])
        self.assertIn("implemented perfectly", concept["system_prompt"])

    def test_index_matches_workflow(self):
        index = yaml.safe_load((WORKFLOW.parents[2] / "index.yaml").read_text())
        self.assertEqual(index["workflows"]["pr-review"]["path"], "workflows/pr-review/workflow.yaml")
        self.assertEqual(
            index["workflows"]["pr-review"]["description"],
            self.document["workflow"]["description"],
        )


if __name__ == "__main__":
    unittest.main()
