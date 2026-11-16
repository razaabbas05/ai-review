"""Publish review results to a PR: inline review comments, resolved-thread edits, the summary comment, and the check run.

Every call goes through `gh api`, so it runs with the workflow's GITHUB_TOKEN.
Set DRY_RUN=1 to print the calls instead of making them.
"""
import json
import os

from ghapi import gh_api, render_state

DRY_RUN = os.environ.get("DRY_RUN") == "1"
SEVERITY_ORDER = ["Blocker", "High", "Medium", "Low", "Nit"]
REQ_LABEL = {
    "met": "✅ met",
    "partly_met": "🟡 partly met",
    "not_met": "❌ not met",
    "cant_verify": "❔ can't verify from code",
}


def _call(method, path, payload=None):
    if DRY_RUN:
        print(f"[dry-run] {method} {path}")
        if payload is not None:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        return {}
    return gh_api(path, method, payload) or {}


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def inline_body(f):
    return f"**[{f['severity']} · {f['category']}]** {f['title']}\n\n{f['body']}"


def build_summary(result, ctx, open_findings, resolved_count, state):
    lines = ["## 🤖 AI review", ""]
    if ctx.get("jira_note"):
        lines += [f"> {ctx['jira_note']}", ""]
    lines += [f"**Reviewed:** `{ctx['range']}` ({ctx['mode']})", ""]

    if ctx.get("too_large"):
        lines += [f"The diff has {ctx['diff_lines']} lines, above the limit of {ctx['limit']}, "
                  "so it was not reviewed automatically. Split the PR or review it by hand."]
        return "\n".join(lines) + "\n\n" + render_state(state)

    if result.get("summary"):
        lines += [result["summary"], ""]

    lines.append("### Requirements")
    reqs = result.get("requirements", [])
    if reqs:
        lines += ["| ID | Requirement | Status | Evidence |", "|---|---|---|---|"]
        for r in reqs:
            status = REQ_LABEL.get(r.get("status"), REQ_LABEL["cant_verify"])
            lines.append(f"| {_cell(r.get('id', ''))} | {_cell(r.get('text', ''))} | "
                         f"{status} | {_cell(r.get('evidence', ''))} |")
    else:
        lines.append("No requirements to check (no Jira ticket, or none listed).")

    counts = {s: 0 for s in SEVERITY_ORDER}
    for f in open_findings:
        counts[f["severity"]] += 1
    lines += ["", "### Open findings", " · ".join(f"{s}: {counts[s]}" for s in SEVERITY_ORDER)]
    if resolved_count:
        lines.append(f"Resolved since the last review: {resolved_count}")

    unplaced = [f for f in open_findings if not f["inline"]]
    if unplaced:
        lines += ["", "Not attached to a changed line:"]
        lines += [f"- **{f['id']} [{f['severity']} · {f['category']}]** {f['path']}:{f['line']}: {f['title']}"
                  for f in unplaced]
    return "\n".join(lines) + "\n\n" + render_state(state)


def publish(result, ctx):
    """result: validated review output. ctx: repo, pr, head_sha, range, mode, summary_id, prior, limit,
    diff_lines, too_large, jira_note."""
    repo, pr, head = ctx["repo"], ctx["pr"], ctx["head_sha"]
    prior_findings = (ctx.get("prior") or {}).get("findings", [])
    resolved_now = {p["id"] for p in result.get("previous_findings", []) if p.get("status") == "resolved"}

    # 1. Carry earlier findings forward; mark resolved ones and edit their inline comments.
    findings, resolved_count = [], 0
    for old in prior_findings:
        f = dict(old)
        if f["status"] == "open" and f["id"] in resolved_now:
            f["status"] = "resolved"
            resolved_count += 1
            if f.get("comment_id"):
                _call("PATCH", f"repos/{repo}/pulls/comments/{f['comment_id']}", {
                    "body": f"✅ **Resolved in {head[:7]}**\n\n~~{f['severity']} · {f['category']}: {f['title']}~~",
                })
        findings.append(f)

    # 2. New findings: one review holds all inline comments, so the developer gets one notification.
    next_no = max([int(f["id"].split("-")[1]) for f in findings] + [0]) + 1
    new = []
    for nf in result.get("new_findings", []):
        f = {"id": f"F-{next_no}", "path": nf["path"], "line": nf["line"], "severity": nf["severity"],
             "category": nf["category"], "title": nf["title"], "status": "open",
             "comment_id": None, "inline": nf["placed"]}
        next_no += 1
        new.append((f, nf))
        findings.append(f)

    inline = [(f, nf) for f, nf in new if f["inline"]]
    if inline:
        review = _call("POST", f"repos/{repo}/pulls/{pr}/reviews", {
            "commit_id": head,
            "event": "COMMENT",  # never APPROVE or REQUEST_CHANGES: humans decide
            "body": "AI review: inline findings for this push. Summary is in the AI review comment.",
            "comments": [{"path": f["path"], "line": f["line"], "side": "RIGHT", "body": inline_body(nf)}
                         for f, nf in inline],
        })
        if not DRY_RUN and review.get("id"):
            posted = gh_api(f"repos/{repo}/pulls/{pr}/reviews/{review['id']}/comments", paginate=True) or []
            ids = {(c["path"], c["line"]): c["id"] for c in posted}
            for f, _ in inline:
                f["comment_id"] = ids.get((f["path"], f["line"]))

    # 3. Summary comment: edit the existing one, or create it on the first run.
    open_findings = [f for f in findings if f["status"] == "open"]
    state = {"last_sha": head, "findings": findings}
    body = build_summary(result, ctx, open_findings, resolved_count, state)
    if ctx.get("summary_id"):
        _call("PATCH", f"repos/{repo}/issues/comments/{ctx['summary_id']}", {"body": body})
    else:
        _call("POST", f"repos/{repo}/issues/{pr}/comments", {"body": body})

    # 4. Check run: informational. It is not added to branch protection, so it never blocks a merge.
    if ctx.get("too_large"):
        conclusion = "neutral"
    elif any(f["severity"] in ("Blocker", "High") for f in open_findings):
        conclusion = "failure"
    elif open_findings:
        conclusion = "neutral"
    else:
        conclusion = "success"
    _call("POST", f"repos/{repo}/check-runs", {
        "name": "AI Review",
        "head_sha": head,
        "status": "completed",
        "conclusion": conclusion,
        "output": {
            "title": f"AI review: {len(open_findings)} open finding(s)",
            "summary": result.get("summary") or "See the AI review summary comment on this PR.",
        },
    })
