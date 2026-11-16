#!/usr/bin/env python3
"""Write the PR's Jira ticket to .ai-context/jira.md, and screenshots to .ai-context/attachments/.

Live mode needs:  JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_KEY
Offline mode:     JIRA_FIXTURE=path/to/saved_issue.json   (no network; used by the tests)
"""
import base64
import json
import os
import pathlib
import urllib.request

MAX_ATTACHMENTS = 10
MAX_IMAGE_BYTES = 5_000_000
FIELDS = "summary,description,comment,attachment,issuelinks,issuetype,status"


def _live_get(path):
    base = os.environ["JIRA_BASE_URL"].rstrip("/")
    creds = f"{os.environ['JIRA_EMAIL']}:{os.environ['JIRA_API_TOKEN']}"
    req = urllib.request.Request(base + path, headers={
        "Authorization": "Basic " + base64.b64encode(creds.encode()).decode(),
        "Accept": "application/json, */*",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def load_issue(key):
    fixture = os.environ.get("JIRA_FIXTURE")
    if fixture:
        return json.loads(pathlib.Path(fixture).read_text())
    return json.loads(_live_get(f"/rest/api/2/issue/{key}?fields={FIELDS}"))


def render(key, issue, out_dir, live):
    f = issue["fields"]
    lines = [
        f"# {key}: {f['summary']}",
        f"Type: {f['issuetype']['name']} | Status: {f['status']['name']}",
        "",
        "## Description",
        f.get("description") or "(none)",
        "",
        "## Comments",
    ]
    comments = (f.get("comment") or {}).get("comments", [])
    for c in comments:
        lines.append(f"- {c['author']['displayName']} ({c['created'][:10]}): {c['body']}")
    if not comments:
        lines.append("(none)")

    lines += ["", "## Linked issues"]
    links = f.get("issuelinks", [])
    for link in links:
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if other:
            summary = other.get("fields", {}).get("summary", "")
            lines.append(f"- {link['type']['name']} {other['key']}: {summary}")
    if not links:
        lines.append("(none)")

    lines += ["", "## Attachments"]
    attachments = f.get("attachment", [])[:MAX_ATTACHMENTS]
    if not attachments:
        lines.append("(none)")
    for a in attachments:
        name = pathlib.Path(a["filename"]).name  # never let a filename write outside the folder
        mime, size = a["mimeType"], a["size"]
        if live and mime.startswith("image/") and size <= MAX_IMAGE_BYTES:
            dest = out_dir / "attachments" / f"{a['id']}_{name}"
            dest.write_bytes(_live_get(f"/rest/api/2/attachment/content/{a['id']}"))
            lines.append(f"- {name} -> {dest} (screenshot; open it with Read)")
        else:
            lines.append(f"- {name} ({mime}, not downloaded)")
    return "\n".join(lines) + "\n"


def main():
    key = os.environ["JIRA_KEY"]
    out_dir = pathlib.Path(os.environ.get("AI_CONTEXT_DIR", ".ai-context"))
    (out_dir / "attachments").mkdir(parents=True, exist_ok=True)
    live = not os.environ.get("JIRA_FIXTURE")
    md = render(key, load_issue(key), out_dir, live)
    (out_dir / "jira.md").write_text(md)
    print(f"Wrote {out_dir / 'jira.md'} ({len(md)} chars, live={live})")


if __name__ == "__main__":
    main()
