"""Render and recover distinct findings without treating the summary as feedback."""

import base64
import json
import re


MARKER = re.compile(r"\n\n<!-- conductor-review:v([12]):([A-Za-z0-9_=-]+) -->$")


def location_of(item):
    location = item["path"]
    if location and item["line"]:
        location += f":{item['line']}"
    return location


def visible_body(findings, opening="", confirmed=()):
    inline_count = sum(item["placement"] == "inline" for item in findings)
    thread_count = sum(item["placement"] == "thread" for item in findings)
    body_count = len(findings) - inline_count - thread_count
    summary = f"{len(findings)} approved findings: {inline_count} inline, "
    if thread_count:
        summary += f"{thread_count} as replies on existing threads, "
    parts = [summary + f"{body_count} in this review body."]
    if opening:
        parts.insert(0, opening)
    if thread_count:
        lines = [f"- `{location_of(i) or 'General'}`: {i['title']}" for i in findings if i["placement"] == "thread"]
        parts.append("Still open, replied on the original thread:\n\n" + "\n".join(lines))
    if confirmed:
        lines = [f"- `{location_of(i) or 'General'}`: {i['title']}" for i in confirmed]
        parts.append("Fix confirmed and thread resolved on the original comment:\n\n" + "\n".join(lines))
    for item in findings:
        if item["placement"] == "body":
            location = location_of(item)
            parts.append(f"### {location or 'General'}\n\n{item['body']}")
    return "\n\n".join(parts)


def render(findings, opening="", confirmed=()):
    """Include a manifest whose visible body can be verified before deduplication."""
    manifest = {"opening": opening, "findings": findings}
    if confirmed:
        manifest["confirmed"] = list(confirmed)
    encoded = base64.urlsafe_b64encode(json.dumps(manifest, ensure_ascii=True).encode()).decode()
    return visible_body(findings, opening, confirmed) + f"\n\n<!-- conductor-review:v2:{encoded} -->"


def recover(body):
    """Return verified provenance, or None so edited/unrecognized text stays reviewable."""
    match = MARKER.search(body)
    if not match:
        return None
    try:
        data = json.loads(base64.b64decode(match[2], altchars=b"-_", validate=True))
        opening, confirmed = "", []
        if match[1] == "1":
            findings = data
        else:
            if not isinstance(data, dict) or not {"opening", "findings"} <= set(data) <= {"opening", "findings", "confirmed"}:
                return None
            opening, findings = data["opening"], data["findings"]
            confirmed = data.get("confirmed", [])
            if not isinstance(opening, str) or not isinstance(confirmed, list) or any(
                not isinstance(c, dict) or not all(isinstance(c.get(k), str) for k in ("path", "title"))
                or type(c.get("line")) is not int for c in confirmed
            ):
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
                or item.get("placement") not in ("inline", "body", "thread")
                or (item["placement"] == "thread" and not isinstance(item.get("title"), str))
                or not isinstance(item.get("path"), str)
                or type(item.get("line")) is not int
                or not isinstance(item.get("body"), str)
                or not item["body"].strip()
            ):
                return None
            ids.add(item["finding_id"])
        if visible_body(findings, opening, confirmed) != body[:match.start()]:
            return None
        return findings
    except (ValueError, UnicodeDecodeError):
        return None
