#!/usr/bin/env python3
"""Read an existing pull request without changing authentication or repository state."""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlparse

from github_auth import AuthError, authenticated_login, query


def resolve(reference: str, repository: str, host: str) -> tuple[str, str]:
    reference = reference.strip()
    if re.fullmatch(r"#?[1-9][0-9]*", reference):
        return repository, reference.lstrip("#")
    match = re.fullmatch(r"([\w.-]+/[\w.-]+)#([1-9][0-9]*)", reference)
    if match:
        return match[1], match[2]
    url = urlparse(reference)
    match = re.fullmatch(r"/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)/?", url.path)
    if url.scheme == "https" and url.hostname == host and match and not url.username:
        return match[1], match[2]
    raise ValueError("Expected a PR number, owner/repo#number, or an HTTPS PR URL on the target host.")


def main(argv: list[str]) -> None:
    result = {"found": False, "auth_error": False, "notes": ""}
    try:
        if len(argv) != 3:
            raise ValueError("Expected pull request reference, repository, and host.")
        reference, repository, host = argv
        nwo, number = resolve(reference, repository, host)
        result["name_with_owner"] = nwo
        if nwo.casefold() == repository.casefold():
            env = dict(os.environ)
            login = authenticated_login(env)
            pr = query(["api", f"repos/{nwo}/pulls/{number}"], env)
            result.update({
                "found": True, "pr_number": pr["number"], "pr_title": pr["title"],
                "pr_url": pr["html_url"], "gh_user": login, "author": pr["user"]["login"],
                "base_ref": pr["base"]["ref"],
                "state": "MERGED" if pr["merged"] else str(pr["state"]).upper(),
                "is_draft": pr["draft"],
            })
    except AuthError as exc:
        result.update(found=False, auth_error=True, notes=str(exc))
    except (ValueError, KeyError, TypeError) as exc:
        result.update(found=False, notes=str(exc))
    print(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
