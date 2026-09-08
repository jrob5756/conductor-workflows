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
        self.assertEqual(self.route("select_review", {"followup": True}), "followup_review")
        self.assertEqual(self.route("select_review", {"followup": False}), "concept_review")

    def test_code_review_always_bracketed_by_ci(self):
        self.assertEqual(self.route("concept_review", {"blocking": [], "verdict": "good_addition"}), "ci_start")
        self.assertEqual(self.agents["concept_gate"]["options"][0]["route"], "ci_start")
        self.assertEqual(self.route("ci_start", {"ok": False}), "code_review")
        self.assertEqual(self.route("code_review", {"findings": []}), "ci_wait")
        self.assertEqual(self.route("code_review", {"findings": [{"title": "bug"}]}), "ci_wait")
        self.assertEqual(self.route("ci_wait", {"ok": True}), "build_questions")
        self.assertEqual(self.route("ci_wait", {"ok": False}), "cleanup")

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
