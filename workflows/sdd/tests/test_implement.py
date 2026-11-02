import json
from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "implement.yaml"
IMPLEMENTERS = {
    "coder": "epic_reviewer",
    "epic_fixer": "epic_reviewer",
    "fixer": "plan_reviewer",
}


class ImplementationEscalationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = yaml.safe_load(WORKFLOW.read_text())
        cls.agents = {agent["name"]: agent for agent in cls.document["agents"]}
        cls.jinja = Environment(undefined=StrictUndefined)
        cls.jinja.filters["json"] = json.dumps

    def context(self, history=(), allow_human_escalation=True):
        return {
            "workflow": {"input": {
                "plan": "feature.plan.md",
                "epic": "",
                "allow_human_escalation": allow_human_escalation,
                "max_epic_review_rounds": 4,
            }},
            "context": {"history": list(history)},
            "worktree_setup": {"output": {
                "plan": "/repo.worktrees/feature/feature.plan.md",
                "worktree_path": "/repo.worktrees/feature",
                "branch": "feature/feature",
            }},
            "epic_selector": {"output": {
                "current_epic": "EPIC-002",
                "epic_details": "Implement the next feature.",
                "plan_summary": "A two-epic plan.",
                "prerequisites_met": True,
            }},
            "coder": {"output": {
                "current_epic": "EPIC-002",
                "epic_details": "Implement the next feature.",
            }},
            "epic_reviewer": {"output": {
                "review_round": 2,
                "feedback": "Resolve the boundary case.",
                "blocking_issues": ["Boundary case is not handled."],
                "suggestions": [],
            }},
            "plan_reviewer": {"output": {
                "decision": "REQUEST_CHANGES",
                "feedback": "Resolve the integration issue.",
                "architecture_issues": [],
                "code_issues": ["Integration is incorrect."],
                "documentation_issues": [],
                "test_gaps": [],
                "suggestions": [],
            }},
        }

    def route(self, name, output, context):
        for route in self.agents[name]["routes"]:
            condition = route.get("when")
            if condition is None or self.jinja.from_string(condition).render(
                output=output, **context
            ).strip() == "True":
                return route["to"]
        self.fail(f"No matching route for {name}")

    def request(self, **overrides):
        return {
            "blocked": False,
            "needs_human_escalation": True,
            "escalation_questions": [{
                "question": "Which boundary behavior is required?",
                "choices": ["Reject (Recommended)", "Accept"],
            }],
            **overrides,
        }

    def test_questions_pause_before_review_and_resume_the_requesting_agent(self):
        for name, reviewer in IMPLEMENTERS.items():
            with self.subTest(agent=name):
                gate = f"{name}_questions"
                output = self.request()
                context = self.context()
                self.assertEqual(self.route(name, output, context), gate)
                self.assertEqual(self.agents[gate]["source"], f"{name}.output.escalation_questions")
                self.assertEqual(self.route(gate, {}, context), name)
                context["context"]["history"] = [gate]
                self.assertEqual(
                    self.route(name, self.request(needs_human_escalation=False,
                                                 escalation_questions=[]), context),
                    reviewer,
                )

    def test_unattended_and_empty_requests_preserve_existing_review_paths(self):
        for name, reviewer in IMPLEMENTERS.items():
            for enabled, output in (
                (False, self.request()),
                (True, self.request(needs_human_escalation=False)),
                (True, self.request(escalation_questions=[])),
            ):
                with self.subTest(agent=name, enabled=enabled, output=output):
                    self.assertEqual(
                        self.route(name, output, self.context(allow_human_escalation=enabled)),
                        reviewer,
                    )

    def test_verified_coder_blockers_still_halt_without_review_or_commit(self):
        for enabled in (False, True):
            context = self.context(allow_human_escalation=enabled)
            self.assertEqual(
                self.route("coder", self.request(blocked=True, needs_human_escalation=False), context),
                "epic_blocked",
            )
        self.assertEqual(
            self.route("coder", self.request(blocked=True), self.context()), "coder_questions",
        )
        self.assertEqual(self.route("epic_blocked", {}, self.context()), "$end")

    def test_question_gates_support_back_skip_and_abort_without_committing(self):
        for name in IMPLEMENTERS:
            with self.subTest(agent=name):
                gate = self.agents[f"{name}_questions"]
                self.assertEqual(gate["type"], "questions")
                for flag in ("allow_back", "allow_skip", "allow_skip_all", "allow_abort"):
                    self.assertTrue(gate[flag])
                self.assertEqual(gate["abort_route"], "$end")
                self.jinja.from_string(gate["prompt"]).render(**self.context())

    def test_resume_prompts_include_answers_and_partial_progress(self):
        for name in IMPLEMENTERS:
            with self.subTest(agent=name):
                gate = f"{name}_questions"
                context = self.context(history=[gate])
                context[name] = {"output": {
                    **context.get(name, {}).get("output", {}),
                    "progress": "Existing passing test and partial implementation.",
                }}
                context[gate] = {"output": {
                    "answered_any": True,
                    "transcript": "Use strict rejection at the boundary.",
                }}
                self.assertIn(f"{name}.output?", self.agents[name]["input"])
                self.assertIn(f"{gate}.output?", self.agents[name]["input"])
                prompt = self.jinja.from_string(self.agents[name]["prompt"]).render(**context)
                self.assertIn(context[gate]["output"]["transcript"], prompt)
                self.assertIn(context[name]["output"]["progress"], prompt)

    def test_skipped_answers_and_stale_answers_do_not_require_a_transcript(self):
        for name in IMPLEMENTERS:
            with self.subTest(agent=name):
                gate = f"{name}_questions"
                context = self.context(history=[gate])
                context[name] = {"output": context.get(name, {}).get("output", {})}
                context[gate] = {"output": {"answered_any": False}}
                prompt = self.jinja.from_string(self.agents[name]["prompt"]).render(**context)
                self.assertIn("The human skipped all questions.", prompt)

                context["context"]["history"] = ["epic_selector" if name == "coder" else "epic_reviewer"]
                context[gate]["output"] = {
                    "answered_any": True,
                    "transcript": "Stale decision from a previous epic or fix pass.",
                }
                prompt = self.jinja.from_string(self.agents[name]["prompt"]).render(**context)
                self.assertNotIn(context[gate]["output"]["transcript"], prompt)
                self.assertEqual(self.route(name, self.request(), context), gate)

    def test_human_answer_summary_includes_implementers_and_existing_reviewers(self):
        template = self.jinja.from_string(self.document["output"]["human_questions_answered"])
        self.assertEqual(template.render(), "False")
        for name in (*IMPLEMENTERS, "epic_reviewer", "plan_reviewer"):
            with self.subTest(agent=name):
                self.assertEqual(template.render(**{
                    f"{name}_questions": {"output": {"answered_any": True}},
                }), "True")
                self.assertEqual(template.render(**{
                    f"{name}_questions": {"output": {"answered_any": False}},
                }), "False")

    def test_existing_reviewer_escalation_precedes_approval_and_round_caps(self):
        output = {
            **self.request(),
            "blocking_issues": ["A real defect."],
            "review_round": 4,
        }
        context = self.context()
        self.assertEqual(self.route("epic_reviewer", output, context), "epic_reviewer_questions")
        self.assertEqual(
            self.route("epic_reviewer", {**output, "needs_human_escalation": False}, context),
            "mark_round_cap_reached",
        )
        self.assertEqual(self.route("mark_round_cap_reached", {}, context), "committer")
        self.assertEqual(
            self.route("plan_reviewer", {
                **self.request(),
                "architecture_issues": [], "code_issues": [],
                "documentation_issues": [], "test_gaps": [],
            }, context),
            "plan_reviewer_questions",
        )


if __name__ == "__main__":
    unittest.main()
