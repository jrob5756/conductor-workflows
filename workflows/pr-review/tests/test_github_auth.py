import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import github_auth as auth
import bootstrap
import pr_resolver


def completed(value=None, *, code=0, error=""):
    return subprocess.CompletedProcess(
        [], code, json.dumps(value) if value is not None else "", error,
    )


class AccountSelectionTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.calls = []

    def fake_gh(self, args, env, deadline=None):
        self.calls.append((args, dict(env)))
        if args[:2] == ["auth", "status"]:
            return completed({"hosts": {"github.com": [
                {"login": "personal", "active": False, "state": "success"},
                {"login": "enterprise", "active": True, "state": "success"},
            ], "other.example": [{"login": "unrelated", "active": True}]}})
        if args[:2] == ["auth", "token"]:
            return subprocess.CompletedProcess([], 0, "secret-" + args[-1], "")
        token = env["GH_TOKEN"]
        login = token.removeprefix("secret-")
        if args == ["api", "user"]:
            return completed({"login": login})
        if args == ["api", "repos/owner/repo"]:
            return completed({
                "full_name": "owner/repo", "default_branch": "main",
                "permissions": {"pull": True, "push": login == "personal"},
            })
        self.fail(f"Unexpected GitHub command: {args}")

    def test_wrong_active_account_recovers_without_switch_or_token_output(self):
        with patch.object(auth, "gh", side_effect=self.fake_gh):
            identity, repo = auth.select_account("github.com", "owner/repo")
        self.assertEqual(identity["gh_user"], "personal")
        self.assertEqual(identity["gh_logins"], ["personal", "enterprise"])
        self.assertEqual(identity["auth_source"], "stored")
        self.assertEqual(repo["full_name"], "owner/repo")
        self.assertNotIn("secret-", json.dumps(identity))
        self.assertNotIn("secret-", json.dumps(repo))
        self.assertEqual(os.environ.get("GH_TOKEN"), None)
        self.assertNotIn(["auth", "switch"], [args[:2] for args, _ in self.calls])
        tokens = [args[-1] for args, _ in self.calls if args[:2] == ["auth", "token"]]
        self.assertEqual(tokens, ["enterprise", "personal"])

    def test_suitable_active_account_is_not_replaced(self):
        def call(args, env, deadline=None):
            if args[:2] == ["auth", "status"]:
                return completed({"hosts": {"github.com": [
                    {"login": "personal", "active": True},
                    {"login": "enterprise", "active": False},
                ]}})
            return self.fake_gh(args, env, deadline)
        with patch.object(auth, "gh", side_effect=call):
            identity, _ = auth.select_account("github.com", "owner/repo")
        self.assertEqual(identity["gh_user"], "personal")
        tokens = [args[-1] for args, _ in self.calls if args[:2] == ["auth", "token"]]
        self.assertEqual(tokens, ["personal"])

    def test_no_write_permission_or_unknown_permissions_never_pass(self):
        for permissions in (None, {}, {"pull": True}, {"push": "true"}):
            def call(args, env, deadline=None):
                if args == ["api", "repos/owner/repo"]:
                    return completed({"permissions": permissions})
                return self.fake_gh(args, env, deadline)
            with self.subTest(permissions=permissions), patch.object(auth, "gh", side_effect=call):
                with self.assertRaisesRegex(auth.AuthError, "No usable GitHub identity.*retry"):
                    auth.select_account("github.com", "owner/repo")

    def test_revoked_first_account_does_not_hide_usable_candidate(self):
        def call(args, env, deadline=None):
            if args[:2] == ["auth", "token"] and args[-1] == "enterprise":
                return completed(code=1, error="credential unavailable")
            return self.fake_gh(args, env, deadline)
        with patch.object(auth, "gh", side_effect=call):
            identity, _ = auth.select_account("github.com", "owner/repo")
        self.assertEqual(identity["gh_user"], "personal")

    def test_nonzero_auth_status_can_still_list_valid_accounts(self):
        def call(args, env, deadline=None):
            response = self.fake_gh(args, env, deadline)
            if args[:2] == ["auth", "status"]:
                response.returncode = 1
            return response
        with patch.object(auth, "gh", side_effect=call):
            self.assertEqual(auth.select_account("github.com", "owner/repo")[0]["gh_user"], "personal")

    def test_no_accounts_or_malformed_inventory_requests_authentication(self):
        for response in (completed({"hosts": {}}), completed([]), completed(code=1)):
            with self.subTest(response=response), patch.object(auth, "gh", return_value=response):
                with self.assertRaises(auth.AuthError):
                    auth.select_account("github.com", "owner/repo")

    def test_explicit_token_wins_and_is_not_replaced_even_on_denial(self):
        for login in ("personal", "enterprise"):
            with self.subTest(login=login), patch.dict(os.environ, {"GH_TOKEN": f"secret-{login}"}):
                self.calls.clear()
                with patch.object(auth, "gh", side_effect=self.fake_gh):
                    if login == "personal":
                        identity, _ = auth.select_account("github.com", "owner/repo")
                        self.assertEqual(identity["auth_source"], "environment")
                    else:
                        with self.assertRaisesRegex(auth.AuthError, "explicit token and restart"):
                            auth.select_account("github.com", "owner/repo")
                self.assertFalse(any(args[:2] == ["auth", "token"] for args, _ in self.calls))

    def test_pinning_ignores_global_account_and_scrubs_unrelated_environment(self):
        with patch.dict(os.environ, {
            "GH_DEBUG": "api", "GH_REPO": "other/repo", "GITHUB_TOKEN": "wrong",
            "GH_ENTERPRISE_TOKEN": "unrelated", "GH_HOST": "other.example",
        }), patch.object(auth, "gh", side_effect=self.fake_gh):
            env = auth.pinned_environment("github.com", "personal", "stored")
            self.assertEqual(env["GH_TOKEN"], "secret-personal")
            self.assertEqual(env["GH_HOST"], "github.com")
            self.assertEqual(env["PR_REVIEW_GH_HOST"], "github.com")
            for key in ("GH_DEBUG", "GH_REPO", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
                self.assertNotIn(key, env)
            self.assertEqual(os.environ["GITHUB_TOKEN"], "wrong")
        self.assertFalse(any(args[:2] == ["auth", "status"] for args, _ in self.calls))

    def test_changed_identity_fails_before_executing_target(self):
        with patch.dict(os.environ, {"GH_TOKEN": "secret-enterprise"}), patch.object(
            auth, "gh", side_effect=self.fake_gh,
        ), patch.object(auth.os, "execve") as execute, contextlib.redirect_stdout(io.StringIO()) as out:
            auth.main(["github.com", "personal", "environment", "ci.py", "start"])
        execute.assert_not_called()
        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertTrue(result["auth_error"])
        self.assertNotIn("secret-", out.getvalue())

    def test_script_receives_credential_only_in_environment(self):
        with patch.object(auth, "gh", side_effect=self.fake_gh), patch.object(
            auth.os, "execve",
        ) as execute, contextlib.redirect_stdout(io.StringIO()) as out:
            auth.main(["github.com", "personal", "stored", "/scripts/post_review.py", "owner/repo", "1"])
        executable, args, env = execute.call_args.args
        self.assertEqual(executable, sys.executable)
        self.assertEqual(args, [sys.executable, "/scripts/post_review.py", "owner/repo", "1"])
        self.assertEqual(env["GH_TOKEN"], "secret-personal")
        self.assertNotIn("secret-", repr(args))
        self.assertEqual(out.getvalue(), "")

    def test_gh_wrapper_refuses_auth_switch_and_token_commands(self):
        for action in ("switch", "token"):
            with self.subTest(action=action), patch.object(
                auth, "gh", side_effect=self.fake_gh,
            ), patch.object(auth.os, "execvpe") as execute, contextlib.redirect_stdout(io.StringIO()) as out:
                auth.main(["github.com", "personal", "stored", "gh", "auth", action])
                self.assertFalse(json.loads(out.getvalue())["ok"])
                execute.assert_not_called()

    def test_auth_failure_diagnostics_redact_credentials(self):
        with patch.object(auth, "gh", return_value=completed(code=1, error="HTTP 403 token=secret-value")):
            with self.assertRaises(auth.AuthError) as error:
                auth.query(["api", "user"], {"GH_TOKEN": "secret-value"})
        self.assertNotIn("secret-value", str(error.exception))
        self.assertIn("[REDACTED]", str(error.exception))

    def test_timeouts_do_not_expose_credentials(self):
        with patch.object(auth.subprocess, "run", side_effect=subprocess.TimeoutExpired(["gh"], 1)):
            with self.assertRaisesRegex(auth.AuthError, "authentication check"):
                auth.gh(["api", "user"], {"GH_TOKEN": "secret-value"})


class BootstrapTests(unittest.TestCase):
    def test_https_and_ssh_remote_parsing(self):
        for remote in ("https://github.com/owner/repo.git", "git@github.com:owner/repo.git",
                       "ssh://git@github.com/owner/repo.git"):
            with self.subTest(remote=remote):
                self.assertEqual(bootstrap.repository_from_remote(remote), ("github.com", "owner/repo"))

    def test_private_repository_can_select_account_after_ambient_read_failure(self):
        identity = {"gh_user": "personal", "gh_logins": ["personal"], "auth_source": "stored"}
        with patch.object(bootstrap, "run", side_effect=[
            (0, "/src/repo", ""), (1, "", "HTTP 404"),
            (0, "git@github.com:owner/repo.git", ""),
        ]), patch.object(bootstrap, "select_account", return_value=(
            identity, {"full_name": "owner/repo", "default_branch": "main"},
        )) as select, contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                bootstrap.main()
        select.assert_called_once_with("github.com", "owner/repo")
        self.assertTrue(json.loads(out.getvalue())["ok"])

    def test_auth_failure_has_gate_signal_without_review_start(self):
        with patch.object(bootstrap, "run", side_effect=[
            (0, "/src/repo", ""),
            (0, json.dumps({"nameWithOwner": "owner/repo", "url": "https://github.com/owner/repo"}), ""),
        ]), patch.object(bootstrap, "select_account", side_effect=auth.AuthError("No usable account")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                bootstrap.main()
        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertTrue(result["auth_error"])
        self.assertEqual(result["error"], "No usable account")


class PrResolverTests(unittest.TestCase):
    def test_supported_pr_references(self):
        for reference in ("571", "#571", "owner/repo#571", "https://github.com/owner/repo/pull/571"):
            with self.subTest(reference=reference):
                self.assertEqual(pr_resolver.resolve(reference, "owner/repo", "github.com"),
                                 ("owner/repo", "571"))
        for reference in ("0", "-1", "gh auth switch", "https://other.example/owner/repo/pull/571"):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                pr_resolver.resolve(reference, "owner/repo", "github.com")

    def test_foreign_reference_stops_without_reading_or_switching_accounts(self):
        with patch.object(pr_resolver, "query") as query, contextlib.redirect_stdout(io.StringIO()) as out:
            pr_resolver.main(["other/repo#571", "owner/repo", "github.com"])
        self.assertEqual(json.loads(out.getvalue())["name_with_owner"], "other/repo")
        query.assert_not_called()

    def test_pr_authorization_failure_preserves_retry_gate_signal(self):
        with patch.object(pr_resolver, "authenticated_login", return_value="personal"), patch.object(
            pr_resolver, "query", side_effect=auth.AuthError("Resource not accessible (HTTP 403)"),
        ), contextlib.redirect_stdout(io.StringIO()) as out:
            pr_resolver.main(["#571", "owner/repo", "github.com"])
        result = json.loads(out.getvalue())
        self.assertFalse(result["found"])
        self.assertTrue(result["auth_error"])
        self.assertIn("HTTP 403", result["notes"])

    def test_read_preserves_state_author_and_exact_title(self):
        for state, merged, expected in (("open", False, "OPEN"), ("closed", False, "CLOSED"),
                                        ("closed", True, "MERGED")):
            with self.subTest(state=state, merged=merged), patch.object(
                pr_resolver, "authenticated_login", return_value="personal",
            ), patch.object(pr_resolver, "query", return_value={
                "number": 571, "title": 'Exact "title"', "html_url": "url",
                "user": {"login": "author"}, "base": {"ref": "main"},
                "state": state, "merged": merged, "draft": True,
            }), contextlib.redirect_stdout(io.StringIO()) as out:
                pr_resolver.main(["#571", "owner/repo", "github.com"])
                result = json.loads(out.getvalue())
                self.assertTrue(result["found"])
                self.assertEqual(result["state"], expected)
                self.assertEqual(result["pr_title"], 'Exact "title"')
                self.assertEqual(result["gh_user"], "personal")
                self.assertEqual(result["author"], "author")
                self.assertTrue(result["is_draft"])


if __name__ == "__main__":
    unittest.main()
