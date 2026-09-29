#!/usr/bin/env python3
"""Select a writable GitHub identity and run commands without global account switches."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
COMMAND_TIMEOUT = 15
PREFLIGHT_TIMEOUT = 90


class AuthError(RuntimeError):
    pass


def is_auth_failure(message: str) -> bool:
    """Recognize explicit authentication or authorization refusals, not transport errors."""
    return bool(re.search(
        r"\bHTTP (?:401|403)\b|bad credentials|resource not accessible|"
        r"as an enterprise managed user|must have .* rights to repository",
        message, re.IGNORECASE,
    ))


def token_variables(host: str) -> tuple[str, str]:
    if host == "github.com" or host.endswith(".ghe.com"):
        return "GH_TOKEN", "GITHUB_TOKEN"
    return "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"


def scrub(message: str, env: dict[str, str]) -> str:
    for name in TOKEN_VARIABLES:
        if env.get(name):
            message = message.replace(env[name], "[REDACTED]")
    return message


def gh(args: list[str], env: dict[str, str], deadline: float | None = None) -> subprocess.CompletedProcess:
    timeout = COMMAND_TIMEOUT
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise AuthError("GitHub authentication preflight timed out.")
    try:
        return subprocess.run(
            ["gh", *args], env=env, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthError("GitHub CLI could not complete the authentication check.") from exc


def query(args: list[str], env: dict[str, str], deadline: float | None = None) -> dict:
    proc = gh(args, env, deadline)
    if proc.returncode:
        raise AuthError(scrub(proc.stderr.strip() or "GitHub request failed.", env))
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AuthError("GitHub authentication check returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise AuthError("GitHub authentication check returned a non-object.")
    return value


def clean_environment() -> dict[str, str]:
    env = dict(os.environ)
    for name in (*TOKEN_VARIABLES, "GH_DEBUG", "GH_REPO", "PR_REVIEW_GH_HOST"):
        env.pop(name, None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def stored_logins(host: str, deadline: float | None = None) -> list[str]:
    proc = gh(["auth", "status", "--hostname", host, "--json", "hosts"],
              clean_environment(), deadline)
    try:
        data = json.loads(proc.stdout)
        accounts = data["hosts"].get(host, [])
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        raise AuthError(f"Could not list GitHub accounts on {host}; run 'gh auth login'.") from exc
    if not isinstance(accounts, list) or any(not isinstance(item, dict) for item in accounts):
        raise AuthError(f"GitHub account list for {host} is malformed.")
    ordered = sorted(accounts, key=lambda item: item.get("active") is not True)
    logins = []
    for item in ordered:
        login = item.get("login")
        if isinstance(login, str) and login and login.casefold() not in {x.casefold() for x in logins}:
            logins.append(login)
    return logins


def account_environment(host: str, login: str, source: str,
                        deadline: float | None = None) -> dict[str, str]:
    env = clean_environment()
    primary, alternate = token_variables(host)
    if source == "environment":
        token = os.environ.get(primary) or os.environ.get(alternate)
        if not token:
            raise AuthError(f"The explicit GitHub token for {host} is no longer available.")
    elif source == "stored":
        proc = gh(["auth", "token", "--hostname", host, "--user", login], env, deadline)
        token = proc.stdout.strip()
        if proc.returncode or not token:
            raise AuthError(f"Could not load the stored credential for {login} on {host}.")
    else:
        raise AuthError("Unknown GitHub credential source; restart authentication preflight.")
    env[primary] = token
    env["GH_HOST"] = host
    return env


def authenticated_login(env: dict[str, str], deadline: float | None = None) -> str:
    login = query(["api", "user"], env, deadline).get("login")
    if not isinstance(login, str) or not login:
        raise AuthError("GitHub did not return an authenticated user identity.")
    return login


def select_account(host: str, repository: str) -> tuple[dict, dict]:
    """Return nonsecret identity metadata and repository details, requiring write access."""
    deadline = time.monotonic() + PREFLIGHT_TIMEOUT
    explicit = any(os.environ.get(name) for name in token_variables(host))
    logins = stored_logins(host, deadline)
    candidates = [""] if explicit else logins
    source = "environment" if explicit else "stored"
    errors = []
    for candidate in candidates:
        try:
            env = account_environment(host, candidate, source, deadline)
            login = authenticated_login(env, deadline)
            if candidate and login.casefold() != candidate.casefold():
                raise AuthError("The stored credential belongs to a different GitHub identity.")
            repo = query(["api", f"repos/{repository}"], env, deadline)
            permissions = repo.get("permissions")
            if not isinstance(permissions, dict) or not any(
                permissions.get(key) is True for key in ("push", "maintain", "admin")
            ):
                raise AuthError(f"{login} does not have repository write access.")
            identities = [login, *(item for item in logins if item.casefold() != login.casefold())]
            return {
                "gh_user": login, "gh_logins": identities, "auth_source": source,
                "auth_notice": f"Using {login} on {host}; the global gh account is unchanged.",
            }, repo
        except AuthError as exc:
            errors.append(f"{candidate or 'Explicit environment token'}: {exc}")
    details = " ".join(errors) or f"No stored GitHub accounts were found on {host}."
    remedy = (
        "Fix the explicit token and restart the workflow; it is never replaced by a stored account."
        if explicit else f"Run 'gh auth login --hostname {host}' with a writable account, then retry."
    )
    raise AuthError(f"No usable GitHub identity for {repository}. {details} {remedy}")


def pinned_environment(host: str, login: str, source: str) -> dict[str, str]:
    env = account_environment(host, login, source)
    if authenticated_login(env).casefold() != login.casefold():
        raise AuthError(f"The GitHub credential no longer belongs to {login}; nothing was executed.")
    env["PR_REVIEW_GH_HOST"] = host
    return env


def main(argv: list[str]) -> None:
    try:
        if len(argv) < 4:
            raise AuthError("Expected host, login, credential source, and script path (or gh).")
        host, login, source, target, *args = argv
        env = pinned_environment(host, login, source)
        if target == "gh":
            # Authentication changes are never needed inside a pinned execution.
            if not args or args[0] not in {"api", "pr", "repo", "issue", "run", "workflow"}:
                raise AuthError("Only GitHub repository operations are accepted by this wrapper.")
            os.execvpe("gh", ["gh", *args], env)
        else:
            os.execve(sys.executable, [sys.executable, target, *args], env)
    except (AuthError, OSError) as exc:
        message = str(exc) if isinstance(exc, AuthError) else "Could not start the authenticated command."
        print(json.dumps({
            "ok": False, "auth_error": True, "error": message, "found": False,
            "notes": message, "summary": message, "issues": [message], "can_merge": False,
        }))


if __name__ == "__main__":
    main(sys.argv[1:])
