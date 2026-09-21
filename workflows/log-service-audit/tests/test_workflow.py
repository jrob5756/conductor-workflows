import json
from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


ROOT = Path(__file__).resolve().parents[1]


class IncludeLoader(yaml.SafeLoader):
    pass


IncludeLoader.add_constructor(
    "!file", lambda loader, node: (ROOT / loader.construct_scalar(node)).read_text(encoding="utf-8"),
)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parent = yaml.load((ROOT / "workflow.yaml").read_text(encoding="utf-8"), Loader=IncludeLoader)
        cls.child = yaml.load((ROOT / "candidate.yaml").read_text(encoding="utf-8"), Loader=IncludeLoader)
        cls.agents = {a["name"]: a for a in cls.parent["agents"]}
        cls.child_agents = {a["name"]: a for a in cls.child["agents"]}
        cls.jinja = Environment(undefined=StrictUndefined)

    def route(self, name, output, child=False):
        agent = (self.child_agents if child else self.agents)[name]
        for route in agent["routes"]:
            if "when" not in route or self.jinja.from_string(route["when"]).render(output=output) == "True":
                return route["to"]
        self.fail(f"No matching route for {name} with {output}")

    def test_discovery_is_isolated_parallel_and_consolidated_before_queue(self):
        group = self.parent["parallel"][0]
        self.assertEqual(
            set(group["agents"]),
            {"discover_bugs", "discover_dead_code", "discover_stability", "discover_performance"},
        )
        self.assertEqual(group["failure_mode"], "fail_fast")
        self.assertEqual(group["routes"], [{"to": "consolidate"}])
        self.assertEqual(self.agents["consolidate"]["routes"], [{"to": "queue"}])
        for name in group["agents"]:
            agent = self.agents[name]
            self.assertEqual(agent["model"], "gpt-6-astra")
            self.assertIn("Read-only:", agent["prompt"])
            self.assertNotIn("discovery.outputs", agent["prompt"])

    def test_preflight_gates_prepare_and_costs_no_agent_time(self):
        self.assertEqual(self.agents["preflight"]["type"], "script")
        self.assertEqual(self.route("bootstrap", {"exit_code": 0, "recovering": False}), "preflight")
        self.assertEqual(self.route("preflight", {"exit_code": 0, "ok": True}), "prepare")
        self.assertEqual(self.route("preflight", {"exit_code": 0, "ok": False}), "preflight_failed")
        self.assertEqual(self.route("preflight", {"exit_code": 1, "ok": False}), "preflight_failed")
        self.assertEqual(self.agents["preflight_failed"]["status"], "failed")
        order = [a["name"] for a in self.parent["agents"]]
        self.assertLess(order.index("preflight"), order.index("prepare"))

    def test_candidates_are_serial_subworkflows_and_fail_fast(self):
        group = self.parent["for_each"][0]
        self.assertEqual(group["max_concurrent"], 1)
        self.assertEqual(group["failure_mode"], "fail_fast")
        self.assertEqual(group["source"], "queue.output.candidates")
        self.assertEqual(group["agent"]["type"], "workflow")
        self.assertEqual(group["agent"]["workflow"], "candidate.yaml")
        self.assertEqual(group["agent"]["input_mapping"]["candidate_id"], "{{ candidate.id }}")
        self.assertEqual(group["routes"], [{"to": "finish"}])

    def test_recovery_skips_preflight_preparation_and_discovery(self):
        self.assertEqual(self.route("bootstrap", {"exit_code": 0, "recovering": True}), "queue")
        self.assertEqual(self.route("bootstrap", {"exit_code": 0, "finalized": True}), "already_finished")
        saved = {"preparation": {}, "discoveries": {}, "discovery": {}}
        rendered = self.jinja.from_string(self.agents["queue"]["stdin"]).render(
            bootstrap={"output": {"recovering": True, "saved_plan": saved}},
        )
        self.assertEqual(json.loads(rendered), saved)

    def test_empty_queue_goes_directly_to_finish(self):
        self.assertEqual(self.route("queue", {"exit_code": 0, "candidates": []}), "finish")
        self.assertEqual(self.route("queue", {"exit_code": 0, "candidates": [{"id": "C001"}]}), "candidates")

    def test_candidate_recovery_resumes_at_correct_boundary(self):
        values = {"exit_code": 0, "completed": False, "resolved": False, "has_result": False}
        self.assertEqual(self.route("bootstrap", values, child=True), "reproduce")
        self.assertEqual(self.route("bootstrap", {**values, "has_result": True}, child=True), "select_result")
        self.assertEqual(self.route("bootstrap", {**values, "resolved": True}, child=True), "cleanup")
        self.assertEqual(self.route("bootstrap", {**values, "completed": True}, child=True), "already_done")

    def test_rejected_and_blocked_skip_expensive_review_and_publication(self):
        for status in ("rejected", "blocked"):
            self.assertEqual(self.route("select_result", {"status": status}, child=True), "decline")
        self.assertEqual(self.route("select_result", {"status": "validated"}, child=True), "snapshot")
        self.assertEqual(self.route("decline", {"exit_code": 0}, child=True), "cleanup")

    def test_duplicate_shortlist_precedes_verification(self):
        self.assertEqual(self.route("snapshot", {"exit_code": 0}, child=True), "shortlist")
        self.assertEqual(self.child_agents["shortlist"]["type"], "script")
        self.assertEqual(self.route("shortlist", {"exit_code": 0}, child=True), "verification")
        prompt = self.child_agents["verification"]["prompt"]
        self.assertIn("shortlist.output.shortlist", prompt)
        self.assertIn("shortlist.output.inventory_file", prompt)
        self.assertIn("widen the search", prompt)

    def test_verification_is_required_before_publication(self):
        sources = [
            a["name"] for a in self.child_agents.values()
            if any(r["to"] == "publish" for r in a.get("routes", []))
        ]
        self.assertEqual(sources, ["verification"])

    def test_recoverable_stage_failure_blocks_one_candidate_not_the_run(self):
        for name in ("handoff", "decline", "snapshot", "shortlist", "publish"):
            with self.subTest(stage=name):
                self.assertEqual(self.route(name, {"exit_code": 1}, child=True), "block")
        self.assertEqual(self.route("block", {"exit_code": 0}, child=True), "cleanup")
        self.assertEqual(self.child_agents["cleanup"]["routes"], [{"to": "complete"}])

    def test_only_an_unrestorable_environment_is_fatal(self):
        fatal = [
            name for name, agent in self.child_agents.items()
            if any(r.get("to") == "failed" for r in agent.get("routes", []))
        ]
        self.assertEqual(sorted(fatal), ["block", "bootstrap", "complete"])
        self.assertEqual(self.route("complete", {"exit_code": 1}, child=True), "failed")
        self.assertEqual(self.route("complete", {"exit_code": 0}, child=True), "$end")
        self.assertEqual(self.child_agents["failed"]["status"], "failed")
        self.assertIn("environment", self.child_agents["failed"]["reason"])

    def test_child_select_result_handles_recovery_without_reproducer_context(self):
        rendered = self.jinja.from_string(self.child_agents["select_result"]["value"]).render(
            bootstrap={"output": {"has_result": True, "result": {"id": "C001", "status": "validated"}}},
        )
        self.assertEqual(json.loads(rendered)["id"], "C001")

    def test_publication_gets_only_this_candidates_decision_via_stdin(self):
        rendered = self.jinja.from_string(self.child_agents["publish"]["stdin"]).render(
            verification={"output": {"decision": {"id": "C001", "reason": 'Quote " and newline\n'}}},
            snapshot={"output": {"digest": "abc"}},
        )
        payload = json.loads(rendered)
        self.assertEqual(payload["decision"]["id"], "C001")
        self.assertEqual(set(payload), {"decision", "snapshot_digest"})

    JINJA = __import__("re").compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", __import__("re").S)
    REFERENCE = __import__("re").compile(r"\b([A-Za-z_]\w*\.outputs?(?:\.\w+)*)")
    INPUT_REFERENCE = __import__("re").compile(r"\bworkflow\.input\.(\w+)")

    @classmethod
    def expressions_of(cls, step):
        """Jinja expressions a step evaluates, excluding prose that merely names a path."""
        texts = []
        for key in ("command", "stdin", "working_dir", "prompt", "system_prompt", "value", "reason"):
            if isinstance(step.get(key), str):
                texts.append(step[key])
        texts.extend(item for item in step.get("args", []) if isinstance(item, str))
        for key in ("output_template", "env", "input_mapping", "values"):
            block = step.get(key)
            if isinstance(block, dict):
                texts.extend(value for value in block.values() if isinstance(value, str))
        return [
            (a or b) for text in texts for a, b in cls.JINJA.findall(text)
        ]

    @staticmethod
    def covers(declared, path):
        """Explicit context exposes a declared path and its descendants only."""
        for entry in declared:
            entry = entry.rstrip("?")
            if path == entry or path.startswith(entry + "."):
                return True
        return False

    def assert_declares_references(self, document, step, label):
        declared = step.get("input") or []
        for expression in self.expressions_of(step):
            for path in self.REFERENCE.findall(expression):
                if path.split(".")[0] in ("workflow", "output"):
                    continue
                self.assertTrue(
                    self.covers(declared, path),
                    f"{label} renders '{path}' but input: does not cover it ({declared})",
                )
            for parameter in self.INPUT_REFERENCE.findall(expression):
                self.assertTrue(
                    self.covers(declared, f"workflow.input.{parameter}"),
                    f"{label} renders 'workflow.input.{parameter}' but input: does not cover it",
                )

    def test_path_dependent_inputs_are_declared_optional(self):
        """A required input that only some routes produce fails the whole run."""
        # queue is reachable straight from bootstrap on the recovery route, so
        # preparation and discovery outputs cannot be mandatory there.
        queue = self.agents["queue"]
        for entry in queue["input"]:
            if entry.startswith(("prepare.", "prepare_review.", "discovery.", "consolidate.")):
                self.assertTrue(entry.endswith("?"), f"queue input {entry} must be optional")
        self.assertIn("bootstrap.output.saved_plan?", queue["input"])
        self.assertIn("bootstrap.output.run_dir", queue["input"])
        self.assertIn("bootstrap.output.final_result?", self.agents["already_finished"]["input"])

    def test_every_step_declares_what_it_renders(self):
        for document, origin in ((self.parent, "workflow.yaml"), (self.child, "candidate.yaml")):
            for step in document["agents"]:
                self.assert_declares_references(document, step, f"{origin}:{step['name']}")
            for group in document.get("for_each", []):
                inline = group.get("agent") or {}
                merged = {**inline, "input": inline.get("input") or group.get("input") or []}
                self.assert_declares_references(document, merged, f"{origin}:{group['name']}")

    def test_context_is_explicit_and_every_agent_declares_inputs(self):
        for document in (self.parent, self.child):
            self.assertEqual(document["workflow"]["context"]["mode"], "explicit")
            for agent in document["agents"]:
                if agent.get("type", "agent") != "agent":
                    continue
                self.assertIn("input", agent, f"{agent['name']} must declare its inputs")
                self.assertTrue(agent["input"])

    def test_prompts_never_embed_the_duplicated_script_payload(self):
        for document in (self.parent, self.child):
            for agent in document["agents"]:
                self.assertNotIn("bootstrap.output | tojson", agent.get("prompt") or "")

    def test_all_ai_steps_use_pinned_worktree_shared_rules_and_no_plugins(self):
        for document in (self.parent, self.child):
            for agent in document["agents"]:
                if agent.get("type", "agent") != "agent":
                    continue
                self.assertEqual(agent["working_dir"], "{{ bootstrap.output.worktree }}")
                self.assertEqual(agent["plugins"], [])
                rendered = self.jinja.from_string(agent["system_prompt"]).render(
                    workflow={"dir": "registry"}, bootstrap={"output": {"run_dir": "evidence"}},
                )
                self.assertIn('python "registry\\audit.py" record --run-dir "evidence"', rendered)
                self.assertIn("--scope <local-or-cluster>", rendered)
                self.assertNotIn("{{", rendered)

    def test_early_termination_outputs_have_no_future_dependencies(self):
        context = {
            "workflow": {"input": {"candidate_id": "C001", "run_dir": "run"}},
            "bootstrap": {"output": {"stderr": "error", "run_dir": "run", "final_result": {}}},
            "queue": {"output": {"stderr": "error"}},
            "finish": {"output": {"stderr": "error"}},
            "preflight": {"output": {"stderr": "error", "blockers": ["missing tool"]}},
            "prepare": {"output": {"stderr": "error", "blockers": ["deploy failed"]}},
        }
        for document in (self.parent, self.child):
            for agent in document["agents"]:
                if agent.get("type") == "terminate":
                    for value in agent["output_template"].values():
                        self.jinja.from_string(value).render(**context)

    def test_registry_name_path_and_description_match(self):
        index = yaml.safe_load((ROOT.parents[1] / "index.yaml").read_text(encoding="utf-8"))
        entry = index["workflows"]["log-service-audit"]
        self.assertEqual(self.parent["workflow"]["name"], "log-service-audit")
        self.assertEqual(entry["path"], "workflows/log-service-audit/workflow.yaml")
        self.assertEqual(entry["description"], self.parent["workflow"]["description"])


if __name__ == "__main__":
    unittest.main()
