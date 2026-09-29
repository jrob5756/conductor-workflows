from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "workflow.yaml"


class WorkflowRoutingTests(unittest.TestCase):
    def test_review_and_publication_use_pinned_base(self):
        for name in ("concept_review", "code_review", "followup_review"):
            agent = self.agents[name]
            self.assertIn("pr_worktree.output.base_sha", agent["input"])
            self.assertIn("git diff {{ pr_worktree.output.base_sha }}...HEAD", agent["prompt"])
            self.assertNotIn("{{ pr_worktree.output.remote }}/", agent["prompt"])
        for name in ("post_review", "post_followup_review", "post_approval", "post_note"):
            agent = self.agents[name]
            self.assertIn("pr_worktree.output.base_sha", agent["input"])
            self.assertIn("{{ pr_worktree.output.base_sha }}", agent["args"])

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

    def test_account_selection_is_automatic_and_only_errors_prompt(self):
        self.assertEqual(self.route("bootstrap", {"ok": True, "auth_error": False}), "pr_resolver")
        self.assertEqual(self.route("bootstrap", {"ok": False, "auth_error": True}), "authentication_gate")
        self.assertEqual(self.route("bootstrap", {"ok": False, "auth_error": False}), "bootstrap_failed")
        gate = self.agents["authentication_gate"]
        self.assertEqual(gate["type"], "human_gate")
        self.assertEqual([option["route"] for option in gate["options"]], ["bootstrap_failed", "bootstrap"])
        self.assertEqual(self.agents["pr_resolver"]["type"], "script")
        self.assertNotIn("plugins", self.agents["pr_resolver"])

    def test_every_github_script_uses_the_same_pinned_identity(self):
        scripts = {
            "pr_resolver.py", "pr_worktree.py", "prior_review.py", "ci.py",
            "post_review.py", "merge_readiness.py", "merge_pr.py",
        }
        checked = []
        for name, agent in self.agents.items():
            args = agent.get("args", [])
            if not any(any(argument.endswith("/" + script) for script in scripts) for argument in args):
                continue
            checked.append(name)
            self.assertEqual(args[:4], [
                "{{ workflow.dir }}/scripts/github_auth.py",
                "{{ bootstrap.output.host }}", "{{ bootstrap.output.gh_user }}",
                "{{ bootstrap.output.auth_source }}",
            ], name)
            for field in ("host", "gh_user", "auth_source"):
                self.assertIn(f"bootstrap.output.{field}", agent["input"], name)
        self.assertEqual(set(checked), {
            "pr_resolver", "pr_worktree", "prior_review", "ci_start", "ci_wait",
            "post_review", "post_followup_review", "post_approval", "post_note",
            "merge_readiness", "merge_pr",
        })
        self.assertNotIn("GH_TOKEN", WORKFLOW.read_text())

    def test_reviewers_use_authenticated_reads_without_global_switches(self):
        for name in ("concept_review", "code_review", "followup_review"):
            self.assertIn("scripts/github_auth.py", self.agents[name]["prompt"])
            self.assertIn("Never run `gh auth switch`", self.agents[name]["prompt"])

    def test_ci_authorization_error_pauses_before_review_and_retry_does_not_rebootstrap(self):
        self.assertEqual(
            self.route("ci_start", {"ok": False, "auth_error": True}), "ci_authentication_gate",
        )
        self.assertEqual(self.route("ci_start", {"ok": False, "auth_error": False}), "cleanup")
        gate = self.agents["ci_authentication_gate"]
        self.assertEqual(gate["type"], "human_gate")
        self.assertEqual([option["route"] for option in gate["options"]], ["cleanup", "ci_start"])
        self.assertNotIn("code_review", [option["route"] for option in gate["options"]])
        self.assertEqual(self.route("ci_start", {"ok": True, "auth_error": False}), "followup_review")

    def test_pr_endpoint_and_wrapper_authentication_errors_reach_retry_gate(self):
        for output in (
            {"found": False, "auth_error": True, "notes": "HTTP 403", "name_with_owner": "owner/repo"},
            {"ok": False, "found": False, "auth_error": True, "notes": "Credential expired"},
        ):
            self.assertEqual(self.route("pr_resolver", output), "pr_authentication_gate")
        self.assertEqual(
            [option["route"] for option in self.agents["pr_authentication_gate"]["options"]],
            ["pr_resolver_failed", "bootstrap"],
        )

    def test_merge_wrapper_failure_preserves_diagnostic_through_terminal_inputs(self):
        diagnostic = "Selected GitHub credential expired; nothing was executed."
        context = {
            "pr_resolver": {"output": {"pr_number": 1, "pr_url": "https://github.com/owner/repo/pull/1"}},
            "merge_pr": {"output": {"ok": False, "auth_error": True, "error": diagnostic}},
            "cleanup_report": {"output": {"summary": "Cleanup complete."}},
        }
        self.assertEqual(self.route("cleanup_report", {}, **context), "merge_failed")
        for reference in self.agents["merge_failed"]["input"]:
            agent, output, field = reference.split(".")
            self.assertIn(field, context[agent][output])
        reason = self.jinja.from_string(self.agents["merge_failed"]["reason"]).render(**context)
        self.assertIn(diagnostic, reason)

    def test_code_review_always_bracketed_by_ci(self):
        self.assertEqual(self.route("concept_review", {"blocking": [], "verdict": "good_addition"}), "ci_start")
        self.assertEqual(self.agents["concept_gate"]["options"][0]["route"], "ci_start")
        self.assertEqual(self.route("ci_start", {"ok": False}, concept_review={"output": {}}), "cleanup")
        self.assertEqual(self.route("code_review", {"findings": []}), "ci_wait")
        self.assertEqual(self.route("code_review", {"findings": [{"title": "bug"}]}), "ci_wait")
        self.assertEqual(self.route("ci_wait", {"ok": True}, code_review={"output": {}}), "build_questions")
        self.assertEqual(self.route("ci_wait", {"ok": False}), "cleanup")

    def test_followup_always_starts_and_waits_for_ci(self):
        self.assertEqual(self.route("select_review", {"followup": True}), "ci_start")
        self.assertEqual(self.route("ci_start", {"ok": True}), "followup_review")
        self.assertEqual(self.route("ci_start", {"ok": False}), "cleanup")
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
            followup_review={"output": {"items": [], "duplicate_sources": []}},
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
        self.assertEqual(json.loads(rendered), {
            "code_findings": [], "ci_findings": [{"severity": "BLOCKING", "title": "CI failed"}],
        })

    def test_clean_and_dropped_findings_offer_approval(self):
        self.assertEqual(self.route("build_questions", {"ok": True, "question_count": 0}), "review_clear_gate")
        self.assertEqual(self.route("apply_triage", {"ok": True, "approved_count": 0}), "review_clear_gate")
        self.assertEqual(self.route("apply_triage", {"ok": False}), "cleanup")
        self.assertEqual(self.agents["review_clear_gate"]["options"][0]["route"], "approval_writer")

    def test_approval_and_merge_are_separate(self):
        self.assertEqual(self.route("post_approval", {"ok": True},
                                    workflow={"input": {"merge": True}}), "merge_readiness")
        self.assertEqual(self.route("post_approval", {"ok": True},
                                    workflow={"input": {"merge": False}}), "cleanup")
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

    def test_repository_policy_inputs_and_review_only_exclusions(self):
        import json

        inputs = self.document["workflow"]["input"]
        self.assertEqual(inputs["ignored_checks"]["default"], ["license/cla"])
        self.assertIs(inputs["merge"]["default"], True)
        self.assertEqual(inputs["merge"]["type"], "boolean")
        for excluded in ([], ["license/cla"], ["other/check"]):
            argument = self.jinja.from_string(self.agents["ci_wait"]["args"][-1]).render(
                workflow={"input": {"ignored_checks": excluded}}
            )
            self.assertEqual(json.loads(argument), excluded)
        for name in ("merge_readiness", "merge_pr"):
            self.assertNotIn("workflow.input.ignored_checks", self.agents[name]["input"])
        for name in ("review_clear_gate", "followup_gate", "followup_clear_gate", "post_approval"):
            self.assertIn("workflow.input.merge", self.agents[name]["input"])

    def test_closed_merged_and_unknown_states_stop_before_own_pr_gate(self):
        context = {"bootstrap": {"output": {"name_with_owner": "owner/repo",
                                            "gh_logins": ["me"]}}}
        for state in ("CLOSED", "MERGED", "unknown"):
            output = {"found": True, "name_with_owner": "owner/repo",
                      "state": state, "author": "me", "gh_user": "me"}
            self.assertEqual(self.route("pr_resolver", output, **context), "pr_not_open")
        self.assertEqual(self.agents["pr_not_open"]["type"], "terminate")
        self.assertNotIn("pr_worktree", self.agents["pr_not_open"]["input"])

    def test_both_writers_use_id_body_items_and_publisher_validates(self):
        import json

        approved = [{"id": "b1", "body": "CI failure", "path": "", "line": 0}]
        items = [{"finding_id": "b1", "body": "CI did not pass"}]
        opening = 'Thanks for working on "exports".\nA few points need attention.'
        for writer, publisher, triage in (
            ("comment_writer", "post_review", "apply_triage"),
            ("followup_comment_writer", "post_followup_review", "apply_followup_triage"),
        ):
            self.assertIn("items", self.agents[writer]["output"])
            self.assertNotIn("posted_count", self.agents[writer]["output"])
            self.assertEqual(self.route(writer, {"items": []}), publisher)
            rendered = self.jinja.from_string(self.agents[publisher]["stdin"]).render(
                **{writer: {"output": {"items": items, "opening": opening}},
                   triage: {"output": {"approved": approved}},
                   "build_followup_questions": {"output": {"confirmations": []}}}
            )
            expected = {
                "mode": "findings", "opening": opening, "items": items, "approved": approved,
            }
            if writer == "followup_comment_writer":
                expected["confirmations"] = []
            self.assertEqual(json.loads(rendered), expected)

    def test_concept_note_and_approval_use_distinct_plain_modes(self):
        import json

        rendered = self.jinja.from_string(self.agents["post_review"]["stdin"]).render(
            concept_gate={"output": {"selected": "post_concept"}},
            comment_writer={"output": {"review_body": "Concept finding", "items": []}},
        )
        self.assertEqual(json.loads(rendered), {"mode": "concept", "body": "Concept finding"})
        rendered = self.jinja.from_string(self.agents["post_approval"]["stdin"]).render(
            approval_writer={"output": {"body": 'Looks good. Thanks for fixing "export"!\n'}},
        )
        self.assertEqual(json.loads(rendered), {
            "mode": "approval", "body": 'Looks good. Thanks for fixing "export"!\n',
        })
        rendered = self.jinja.from_string(self.agents["post_note"]["stdin"]).render(
            followup_clear_gate={"output": {"additional_input": {"note": "Verbatim"}}},
        )
        self.assertEqual(json.loads(rendered), {"mode": "note", "body": "Verbatim"})

    def test_all_approval_gates_use_contribution_specific_writer(self):
        for name in ("review_clear_gate", "followup_gate", "followup_clear_gate"):
            approve = next(option for option in self.agents[name]["options"]
                           if option["value"] == "approve")
            expected = "approval_writer" if name == "review_clear_gate" else "confirm_threads_approve"
            self.assertEqual(approve["route"], expected)
            self.assertNotIn("LGTM, thanks for contributing!", self.agents[name]["prompt"])
        self.assertEqual(self.route("confirm_threads_approve", {"ok": False}), "approval_writer")
        self.assertEqual(self.route("confirm_threads_note", {"ok": False}), "post_note")
        self.assertEqual(self.route("approval_writer", {"body": "Looks good, thanks!"}), "post_approval")
        self.assertEqual(self.agents["approval_writer"]["input"], ["pr_resolver.output.pr_title"])
        self.assertIn("approval_writer.output.body", self.agents["post_approval"]["input"])
        prompt = self.jinja.from_string(self.agents["approval_writer"]["prompt"])
        fix = prompt.render(pr_resolver={"output": {"pr_title": "Fix timeout handling"}})
        feature = prompt.render(pr_resolver={"output": {"pr_title": "Add export option"}})
        self.assertIn("Fix timeout handling", fix)
        self.assertIn("Add export option", feature)
        self.assertNotEqual(fix, feature)

    def test_review_writers_request_warm_openings_without_reclassifying_findings(self):
        for name in ("comment_writer", "followup_comment_writer"):
            writer = self.agents[name]
            self.assertEqual(writer["output"]["opening"]["type"], "string")
            self.assertIn("brief, genuine appreciation", writer["system_prompt"])
            self.assertIn("not merge blockers", " ".join(writer["prompt"].split()))
        self.assertIn("including that warm opening", self.agents["comment_writer"]["prompt"])

    def test_fatal_ci_start_routes_to_failed_termination_after_cleanup(self):
        self.assertEqual(self.route("ci_start", {"ok": False}), "cleanup")
        self.assertEqual(self.route("cleanup", {"ok": True}), "cleanup_report")
        self.assertEqual(
            self.route("cleanup_report", {}, ci_start={"output": {"ok": False}}), "ci_failed"
        )
        self.assertEqual(self.agents["ci_failed"]["status"], "failed")

    def test_cleanup_and_posting_terminal_counts_are_truthful(self):
        cleanup = self.jinja.from_string(self.agents["cleanup_report"]["values"]["summary"]).render(
            cleanup={"output": {"ok": False, "worktree_removed": False,
                                "branch_deleted": False, "notes": "Dirty worktree retained."}}
        )
        self.assertIn("not removed", cleanup)
        self.assertIn("Dirty worktree retained", cleanup)
        rendered = self.jinja.from_string(self.agents["review_posted"]["reason"]).render(
            pr_resolver={"output": {"pr_number": 1}},
            apply_triage={"output": {"approved_count": 99, "dropped_count": 0,
                                     "reclassified_count": 0}},
            post_review={"output": {"posted_count": 10, "inline_posted": 8, "body_count": 2,
                                    "inline_demoted": 0, "review_url": "url", "error": ""}},
            cleanup_report={"output": {"summary": cleanup}},
        )
        self.assertIn("10 findings", rendered)
        self.assertIn("8 anchored inline and 2 in the body", rendered)
        self.assertNotIn("worktree has been removed", rendered)
        self.assertIn(cleanup, rendered)
        for route in self.agents["cleanup_report"]["routes"]:
            terminal = self.agents[route["to"]]
            self.assertIn("cleanup_report.output.summary", terminal["input"])


    def test_writer_prompt_renders_findings_without_thread_fields(self):
        prompt = self.jinja.from_string(self.agents["followup_comment_writer"]["prompt"])
        base = {"id": "b1", "severity": "BLOCKING", "title": "T", "path": "", "line": 0,
                "status": "new", "body": "B", "suggestion": "", "guidance": ""}
        threaded = {**base, "id": "b2", "thread_id": "T1", "thread_resolved": True}
        rendered = prompt.render(
            pr_resolver={"output": {"pr_number": 1, "pr_title": "x"}},
            prior_review={"output": {"last_review_at": "t", "last_interaction_at": "t"}},
            apply_followup_triage={"output": {"approved": [base, threaded], "approved_count": 2,
                                              "blocking_count": 2, "recommended_count": 0}},
            build_followup_questions={"output": {"confirmations": []}},
        )
        self.assertEqual(rendered.count("a reply on your original comment's thread"), 1)
        self.assertIn("reopens", rendered)


    def test_blocking_points_after_triage_never_offer_approval(self):
        self.assertEqual(
            self.route("apply_followup_triage", {"ok": True, "approved_count": 2, "blocking_count": 1}),
            "followup_blocking_gate",
        )
        self.assertEqual(
            self.route("apply_followup_triage", {"ok": True, "approved_count": 2, "blocking_count": 0}),
            "followup_gate",
        )
        values = [o["value"] for o in self.agents["followup_blocking_gate"]["options"]]
        self.assertEqual(values, ["comment", "stop"])
        self.assertNotIn("approval_writer", str(self.agents["followup_blocking_gate"]["options"]))

    def test_triage_prompts_stay_short_so_first_question_is_visible(self):
        for name in ("triage", "followup_triage"):
            self.assertLess(len(self.agents[name]["prompt"].splitlines()), 12)


if __name__ == "__main__":
    unittest.main()
