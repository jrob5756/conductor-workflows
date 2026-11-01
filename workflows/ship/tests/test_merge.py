import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge.py"
SPEC = importlib.util.spec_from_file_location("ship_merge_test_module", SCRIPT)
merge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge)

HEAD = "a" * 40
TLS = 'Post "https://api.github.com/graphql": net/http: TLS handshake timeout'


def pr(**changes):
    return {
        "state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "headRefName": "feature/1-x", "headRefOid": HEAD, "author": {"login": "Me"},
        "url": "https://github.com/o/r/pull/7", "mergeCommit": None, **changes,
    }


MERGED = pr(state="MERGED", mergeCommit={"oid": "c" * 40})


class FakeGit:
    """Scripted gh/git: `views` are successive `gh pr view` payloads."""

    def __init__(self, views, merge_results=((0, "", ""),), dirty="", local_sha=HEAD,
                 ancestor=True, remote_listed=True, issue_state="CLOSED", merge_tree=(0, "tree\n", "")):
        self.views = list(views)
        self.merge_results = list(merge_results)
        self.dirty, self.local_sha, self.ancestor = dirty, local_sha, ancestor
        self.remote_listed, self.issue_state, self.merge_tree = remote_listed, issue_state, merge_tree
        self.calls = []

    def __call__(self, args, cwd=None):
        self.calls.append(args)
        if args[:3] == ["gh", "pr", "view"]:
            view = self.views.pop(0) if len(self.views) > 1 else self.views[0]
            return 0, json.dumps(view), ""
        if args[:3] == ["gh", "pr", "merge"]:
            return self.merge_results.pop(0) if len(self.merge_results) > 1 else self.merge_results[0]
        if args[:3] == ["gh", "issue", "view"]:
            return 0, json.dumps({"state": self.issue_state}), ""
        sub = args[3] if args[:2] == ["git", "-C"] else ""
        if sub == "status":
            return 0, self.dirty if "--untracked-files=no" not in args else "", ""
        if sub == "rev-parse":
            return (0, self.local_sha, "") if self.local_sha else (1, "", "")
        if sub == "merge-base":
            return (0 if self.ancestor else 1), "", ""
        if sub == "ls-remote":
            return 0, f"{HEAD}\trefs/heads/feature/1-x" if self.remote_listed else "", ""
        if sub == "merge-tree":
            return self.merge_tree
        if sub == "branch" and "--show-current" in args:
            return 0, "main", ""
        return 0, "", ""

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]

    def git(self, sub):
        return [c for c in self.calls if c[:2] == ["git", "-C"] and c[3] == sub]


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(merge.time, "sleep"))
        self.worktree = self.enterContext(tempfile.TemporaryDirectory())

    def ship(self, fake, gh_user="me"):
        with patch.object(merge, "run", fake):
            return merge.main(["o/r", "7", "feature/1-x", self.worktree, "/repo", "main", "1", gh_user])

    def test_clean_merge_squashes_and_cleans_up(self):
        fake = FakeGit([pr(), MERGED])
        out = self.ship(fake)
        self.assertEqual((out["ok"], out["merged"], out["error"]), (True, True, ""))
        self.assertEqual(out["merge_commit"], "c" * 40)
        self.assertEqual(fake.ran("gh", "pr", "merge"), [["gh", "pr", "merge", "7", "-R", "o/r", "--squash"]])
        self.assertTrue(out["worktree_removed"] and out["branch_deleted"] and out["issue_closed"])
        self.assertIn(["git", "-C", "/repo", "worktree", "remove", self.worktree], fake.calls)
        self.assertIn(["git", "-C", "/repo", "branch", "-D", "feature/1-x"], fake.calls)
        self.assertIn(["git", "-C", "/repo", "push", "--quiet", "origin", "--delete", "feature/1-x"], fake.calls)
        self.assertFalse(any("--force" in c for c in fake.calls))

    def test_refused_merge_is_an_error_not_success(self):
        # The regression: nothing merged, no conflict, and the run still "completed".
        fake = FakeGit([pr()], merge_results=[(1, "", "Required status check \"Lint\" is expected.")])
        out = self.ship(fake)
        self.assertFalse(out["merged"] or out["blocked_by_conflicts"] or out["ok"])
        self.assertIn("Required status check", out["error"])
        self.assertFalse(fake.git("worktree") or fake.git("push"))

    def test_conflicts_are_reported_with_files_and_nothing_is_merged(self):
        fake = FakeGit([pr(mergeable="CONFLICTING")], merge_tree=(1, "tree\nsrc/a.py\nsrc/b.py\n", ""))
        out = self.ship(fake)
        self.assertTrue(out["ok"] and out["blocked_by_conflicts"])
        self.assertFalse(out["merged"])
        self.assertIn("src/a.py, src/b.py", out["conflict_summary"])
        self.assertFalse(fake.ran("gh", "pr", "merge") or fake.git("worktree"))

    def test_merge_rejected_for_conflicts_routes_to_conflicts(self):
        fake = FakeGit([pr(), pr(mergeable="CONFLICTING")], merge_results=[(1, "", "Pull request is not mergeable")])
        out = self.ship(fake)
        self.assertTrue(out["blocked_by_conflicts"])
        self.assertFalse(out["merged"])

    def test_transient_failure_retries_only_after_rereading_state(self):
        fake = FakeGit([pr(), pr(), MERGED], merge_results=[(1, "", TLS), (0, "", "")])
        out = self.ship(fake)
        self.assertTrue(out["merged"])
        self.assertEqual(len(fake.ran("gh", "pr", "merge")), 2)

    def test_lost_response_after_merging_is_not_retried(self):
        fake = FakeGit([pr(), MERGED], merge_results=[(1, "", TLS)])
        out = self.ship(fake)
        self.assertTrue(out["merged"])
        self.assertEqual(len(fake.ran("gh", "pr", "merge")), 1)

    def test_already_merged_pr_is_cleaned_up_without_merging(self):
        fake = FakeGit([MERGED])
        out = self.ship(fake)
        self.assertTrue(out["merged"] and out["worktree_removed"])
        self.assertFalse(fake.ran("gh", "pr", "merge"))
        self.assertIn("already merged", out["cleanup_notes"])

    def test_unknown_mergeability_is_waited_out(self):
        fake = FakeGit([pr(mergeable="UNKNOWN"), pr(mergeable="UNKNOWN"), pr(), MERGED])
        self.assertTrue(self.ship(fake)["merged"])

    def test_dirty_worktree_and_its_branch_are_kept(self):
        fake = FakeGit([pr(), MERGED], dirty=" M notes.txt")
        out = self.ship(fake)
        self.assertTrue(out["merged"])
        self.assertFalse(out["worktree_removed"] or out["branch_deleted"])
        self.assertFalse(fake.git("worktree")[1:] or fake.git("push"))
        self.assertIn("uncommitted", out["cleanup_notes"])

    def test_local_commits_missing_from_the_pr_keep_the_branch(self):
        fake = FakeGit([pr(), MERGED], local_sha="b" * 40, ancestor=False)
        out = self.ship(fake)
        self.assertFalse(out["branch_deleted"])
        self.assertNotIn(["git", "-C", "/repo", "branch", "-D", "feature/1-x"], fake.calls)
        self.assertIn("not in the merged pull request", out["cleanup_notes"])

    def test_someone_elses_pr_is_merged_but_not_cleaned_up(self):
        other = {"author": {"login": "other"}}
        fake = FakeGit([pr(**other), {**MERGED, **other}])
        out = self.ship(fake, gh_user="me")
        self.assertTrue(out["merged"])
        self.assertFalse(fake.git("worktree") or fake.git("push") or fake.git("branch"))

    def test_wrong_branch_is_refused(self):
        out = self.ship(FakeGit([pr(headRefName="other")]))
        self.assertFalse(out["ok"] or out["merged"])
        self.assertIn("not the shipped branch", out["error"])

    def test_closed_pr_is_an_error(self):
        out = self.ship(FakeGit([pr(state="CLOSED")]))
        self.assertIn("closed without merging", out["error"])

    def test_cli_always_prints_one_json_object(self):
        with patch.object(sys, "argv", ["merge.py"]):
            self.assertIn("Usage", merge.main([])["error"])


if __name__ == "__main__":
    unittest.main()
