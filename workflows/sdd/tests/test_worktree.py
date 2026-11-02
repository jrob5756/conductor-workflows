import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


SDD = Path(__file__).resolve().parents[1]
SCRIPT = SDD / "scripts" / "worktree.py"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class WorktreeSetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        origin = self.root / "origin.git"
        seed = self.root / "seed"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        git(self.root, "init", "-q", "-b", "main", str(seed))
        self.configure(seed)
        (seed / "README.md").write_text("seed\n")
        git(seed, "add", "README.md")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "-q", "origin", "main")

        self.repo = self.root / "repo"
        git(self.root, "clone", "-q", str(origin), str(self.repo))
        self.configure(self.repo)
        # A commit only on the local default branch, which the worktree must not contain.
        (self.repo / "local.txt").write_text("unpushed\n")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-q", "-m", "unpushed")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "auth.plan.md").write_text("# Plan\nEPIC-001: TO DO\n")

    def configure(self, path):
        git(path, "config", "user.email", "t@example.com")
        git(path, "config", "user.name", "t")

    def setup_worktree(self, cwd, *args):
        out = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_creates_worktree_from_origin_default_and_copies_plan(self):
        result = self.setup_worktree(self.repo, "docs/auth.plan.md")

        worktree = self.root / "repo.worktrees" / "auth"
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["worktree_path"], result["branch"], result["mode"]),
                         (str(worktree), "feature/auth", "created"))
        self.assertEqual(result["base"], "origin/main")
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "origin/main"))
        self.assertFalse((worktree / "local.txt").exists())
        self.assertTrue(result["plan_copied"])
        self.assertEqual(result["plan"], str(worktree / "docs" / "auth.plan.md"))
        self.assertEqual(Path(result["plan"]).read_text(), "# Plan\nEPIC-001: TO DO\n")
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_rerun_reuses_worktree_and_keeps_its_plan_progress(self):
        first = self.setup_worktree(self.repo, "docs/auth.plan.md")
        Path(first["plan"]).write_text("# Plan\nEPIC-001: DONE\n")

        second = self.setup_worktree(self.repo, "docs/auth.plan.md")

        self.assertEqual((second["worktree_path"], second["mode"]), (first["worktree_path"], "reused"))
        self.assertFalse(second["plan_copied"])
        self.assertEqual(Path(second["plan"]).read_text(), "# Plan\nEPIC-001: DONE\n")
        self.assertIn("differs", second["note"])

    def test_launched_inside_a_worktree_uses_it_as_is(self):
        other = self.root / "repo.worktrees" / "manual"
        git(self.repo, "worktree", "add", "-q", "-b", "fix/manual", str(other), "origin/main")
        (other / "my.plan.md").write_text("# Plan\n")

        result = self.setup_worktree(other, "my.plan.md")

        self.assertEqual((result["worktree_path"], result["branch"], result["mode"]),
                         (str(other), "fix/manual", "launched-in-worktree"))
        self.assertEqual(result["plan"], str(other / "my.plan.md"))
        self.assertFalse(result["plan_copied"])

    def test_branch_input_and_inline_plan(self):
        result = self.setup_worktree(self.repo, "# Plan\nEPIC-001: TO DO\n", "feature/custom")

        self.assertEqual(result["branch"], "feature/custom")
        self.assertEqual(result["worktree_path"], str(self.root / "repo.worktrees" / "custom"))
        self.assertEqual(result["plan"], "# Plan\nEPIC-001: TO DO\n")

    def test_refuses_an_occupied_worktree_path(self):
        (self.root / "repo.worktrees" / "auth").mkdir(parents=True)

        result = self.setup_worktree(self.repo, "docs/auth.plan.md")

        self.assertFalse(result["ok"])
        self.assertIn("already exists", result["error"])

    def test_not_a_git_repository(self):
        outside = self.root / "outside"
        outside.mkdir()

        result = self.setup_worktree(outside, "plan.md")

        self.assertFalse(result["ok"])
        self.assertIn("Not inside a git repository", result["error"])


class WorktreeWiringTests(unittest.TestCase):
    def test_every_agent_runs_in_the_worktree(self):
        document = yaml.safe_load((SDD / "implement.yaml").read_text())
        self.assertEqual(document["workflow"]["entry_point"], "worktree_setup")
        agents = [a for a in document["agents"] if a.get("type", "agent") == "agent"]
        self.assertTrue(agents)
        for agent in agents:
            self.assertEqual(agent.get("working_dir"), "{{ worktree_setup.output.worktree_path }}", agent["name"])
            self.assertNotIn("workflow.input.plan", json.dumps(agent), agent["name"])


if __name__ == "__main__":
    unittest.main()
