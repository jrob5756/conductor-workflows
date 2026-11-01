from pathlib import Path
import unittest

from jinja2 import Environment
import yaml


WORKFLOW = yaml.safe_load((Path(__file__).resolve().parents[1] / "workflow.yaml").read_text())
AGENTS = {agent["name"]: agent for agent in WORKFLOW["agents"]}


def first_route(step, output, autopilot=False, resolver_round=None):
    context = {"output": output, "workflow": {"input": {"autopilot": autopilot, "max_autopilot_rounds": 2}}}
    if resolver_round is not None:
        context["conflict_resolver"] = {"output": {"round": resolver_round}}
    for route in AGENTS[step]["routes"]:
        when = route.get("when")
        if when is None or Environment().from_string(when).render(**context).strip() == "True":
            return route["to"]
    raise AssertionError(f"no route matched for {output}")


class MergeRoutingTests(unittest.TestCase):
    def test_merge_runs_the_script_without_the_skill(self):
        step = AGENTS["merge"]
        self.assertEqual(step["type"], "script")
        self.assertNotIn("plugins", step)
        self.assertTrue(step["args"][0].endswith("/scripts/merge.py"))

    def test_successful_merge_ends_the_run(self):
        self.assertEqual(first_route("merge", {"merged": True, "blocked_by_conflicts": False}), "$end")

    def test_unmerged_without_conflicts_fails_the_run(self):
        for autopilot in (False, True):
            with self.subTest(autopilot=autopilot):
                route = first_route("merge", {"merged": False, "blocked_by_conflicts": False}, autopilot)
                self.assertEqual(route, "merge_failed")
        self.assertEqual(AGENTS["merge_failed"]["type"], "terminate")
        self.assertEqual(AGENTS["merge_failed"]["status"], "failed")

    def test_conflicts_still_reach_the_human_or_the_bounded_resolver(self):
        blocked = {"merged": False, "blocked_by_conflicts": True}
        self.assertEqual(first_route("merge", blocked), "merge_conflicts")
        self.assertEqual(first_route("merge", blocked, autopilot=True), "conflict_resolver")
        self.assertEqual(first_route("merge", blocked, autopilot=True, resolver_round=2), "merge_blocked")


if __name__ == "__main__":
    unittest.main()
