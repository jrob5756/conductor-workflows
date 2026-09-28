"""Render and recover distinct findings without treating the summary as feedback."""

import base64
import json
import re


MARKER = re.compile(r"\n\n<!-- conductor-review:v([12]):([A-Za-z0-9_=-]+) -->$")


def visible_body(findings, opening=""):
    inline_count = sum(item["placement"] == "inline" for item in findings)
    parts = [
        f"{len(findings)} approved findings: {inline_count} inline, "
        f"{len(findings) - inline_count} in this review body."
    ]
    if opening:
        parts.insert(0, opening)
    for item in findings:
        if item["placement"] == "body":
            location = item["path"]
            if location and item["line"]:
                location += f":{item['line']}"
            parts.append(f"### {location or 'General'}\n\n{item['body']}")
    return "\n\n".join(parts)


def render(findings, opening=""):
    """Include a manifest whose visible body can be verified before deduplication."""
    encoded = base64.urlsafe_b64encode(
        json.dumps({"opening": opening, "findings": findings}, ensure_ascii=True).encode()
    ).decode()
    return visible_body(findings, opening) + f"\n\n<!-- conductor-review:v2:{encoded} -->"


def recover(body):
    """Return verified provenance, or None so edited/unrecognized text stays reviewable."""
    match = MARKER.search(body)
    if not match:
        return None
    try:
        data = json.loads(base64.b64decode(match[2], altchars=b"-_", validate=True))
        opening = ""
        if match[1] == "1":
            findings = data
        else:
            if not isinstance(data, dict) or set(data) != {"opening", "findings"}:
                return None
            opening, findings = data["opening"], data["findings"]
            if not isinstance(opening, str):
                return None
        if not isinstance(findings, list) or not findings:
            return None
        ids = set()
        for item in findings:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("finding_id"), str)
                or not item["finding_id"]
                or item["finding_id"] in ids
                or item.get("source_type") not in ("code", "ci")
                or item.get("placement") not in ("inline", "body")
                or not isinstance(item.get("path"), str)
                or type(item.get("line")) is not int
                or not isinstance(item.get("body"), str)
                or not item["body"].strip()
            ):
                return None
            ids.add(item["finding_id"])
        if visible_body(findings, opening) != body[:match.start()]:
            return None
        return findings
    except (ValueError, UnicodeDecodeError):
        return None
