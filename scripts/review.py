#!/usr/bin/env python3
"""One AI review run: gather context, call Claude Code read-only, validate its output, post it.

In CI the reusable workflow sets: REPO, PR_NUMBER, BASE_SHA, HEAD_SHA, PR_TITLE, PR_BODY,
ANTHROPIC_API_KEY, GH_TOKEN. Jira context is read from .ai-context/jira.md (written by jira_context.py).

Local testing, with no GitHub calls and no API calls:
  DIFF_FILE=...          use this diff instead of git
  MOCK_REVIEW_FILE=...   use this model output instead of calling Claude
  PRIOR_STATE_FILE=...   previous state as JSON (instead of reading the PR's comments)
  DRY_RUN=1              print the GitHub calls instead of making them
"""
import json
import os
import pathlib
import re
import subprocess

import post
from diff_utils import parse_diff_lines
from ghapi import find_state, gh_api

PROMPTS = pathlib.Path(__file__).resolve().parent.parent / "prompts"
MAX_DIFF_LINES = int(os.environ.get("MAX_DIFF_LINES", "3000"))
SEVERITIES = {"Blocker", "High", "Medium", "Low", "Nit"}
CATEGORIES = {"requirements", "bug", "security", "performance", "regression", "code quality"}
REQ_STATUSES = {"met", "partly_met", "not_met", "cant_verify"}
PREV_STATUSES = {"resolved", "still_open"}
OFFLINE = bool(os.environ.get("DIFF_FILE"))


def load_prior(repo, pr):
    """Return (summary_comment_id, state) from the previous run, or (None, None) for a first run."""
    if os.environ.get("PRIOR_STATE_FILE"):
        return None, json.loads(pathlib.Path(os.environ["PRIOR_STATE_FILE"]).read_text())
    if OFFLINE:
        return None, None
    return find_state(gh_api(f"repos/{repo}/issues/{pr}/comments", paginate=True))


def compute_diff(prior):
    """Return (diff_text, mode, range_label). Incremental mode reviews only what changed since the last run."""
    if os.environ.get("DIFF_FILE"):
        return pathlib.Path(os.environ["DIFF_FILE"]).read_text(), "full", "fixture"
    head = os.environ["HEAD_SHA"]
    last = (prior or {}).get("last_sha")
    incremental = bool(last) and subprocess.run(
        ["git", "merge-base", "--is-ancestor", last, head], capture_output=True).returncode == 0
    if incremental:
        base, spec, mode = last, f"{last}..{head}", "incremental"
    else:  # first run, or the branch was rebased/force-pushed: review the whole PR
        base = os.environ["BASE_SHA"]
        spec, mode = f"{base}...{head}", "full"
    diff = subprocess.run(["git", "diff", "--no-color", "--no-ext-diff", spec],
                          capture_output=True, text=True, check=True).stdout
    return diff, mode, f"{base[:7]}..{head[:7]}"


def run_claude(prompt, system):
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
    cmd = ["claude", "-p", "--model", model, "--output-format", "json",
           "--append-system-prompt", system,
           "--allowedTools", "Read", "Grep", "Glob",
           "--disallowedTools", "Bash", "Edit", "Write", "WebFetch", "WebSearch"]
    # The prompt goes through stdin: a large diff can exceed the command-line length limit.
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr[-2000:]}")
    return json.loads(proc.stdout).get("result", "")


def extract_json(text):
    """Pull the JSON object out of the model's reply, even if it wrapped it in a code fence or prose."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def validate(data, valid_lines):
    """Normalize the model's output. Mark each finding as placed only if GitHub accepts an inline comment there."""
    out = {"summary": str(data.get("summary", "")), "requirements": [],
           "new_findings": [], "previous_findings": []}
    for r in data.get("requirements", []):
        if isinstance(r, dict) and r.get("id"):
            r = dict(r)
            r["status"] = r.get("status") if r.get("status") in REQ_STATUSES else "cant_verify"
            out["requirements"].append(r)
    for f in data.get("new_findings", []):
        try:
            path, line = str(f["path"]), int(f["line"])
            title, body = str(f["title"]), str(f["body"])
        except (KeyError, TypeError, ValueError):
            continue  # malformed finding: drop it rather than failing the whole run
        out["new_findings"].append({
            "path": path, "line": line, "title": title, "body": body,
            "severity": f.get("severity") if f.get("severity") in SEVERITIES else "Low",
            "category": f.get("category") if f.get("category") in CATEGORIES else "code quality",
            "placed": line in valid_lines.get(path, set()),
        })
    for p in data.get("previous_findings", []):
        if isinstance(p, dict) and p.get("id") and p.get("status") in PREV_STATUSES:
            out["previous_findings"].append(p)
    return out


def build_prompt(guidelines, jira_md, title, body, open_prev, diff):
    prev = json.dumps(open_prev, indent=2) if open_prev else "none"
    return f"""<repo_guidelines>
{guidelines}
</repo_guidelines>

<jira_ticket untrusted="true">
{jira_md or "(no Jira ticket found for this PR)"}
</jira_ticket>

<pull_request untrusted="true">
Title: {title}
Description:
{body}
</pull_request>

<previously_open_findings>
{prev}
</previously_open_findings>

<diff>
{diff}
</diff>

Review the diff as your instructions describe. Output only the JSON object."""


def main():
    repo = os.environ.get("REPO", "local/test")
    pr = os.environ.get("PR_NUMBER", "0")
    head = os.environ.get("HEAD_SHA", "local")
    summary_id, prior = load_prior(repo, pr)
    diff, mode, rng = compute_diff(prior)

    ctx = {"repo": repo, "pr": pr, "head_sha": head, "range": rng, "mode": mode,
           "summary_id": summary_id, "prior": prior, "limit": MAX_DIFF_LINES,
           "diff_lines": len(diff.splitlines()), "too_large": False, "jira_note": ""}

    jira_path = pathlib.Path(".ai-context/jira.md")
    jira_md = jira_path.read_text() if jira_path.exists() else ""
    if not jira_md:
        ctx["jira_note"] = "No Jira ticket linked (no key in the branch name or PR title). Requirements not checked."

    if ctx["diff_lines"] > MAX_DIFF_LINES:
        ctx["too_large"] = True
        post.publish({}, ctx)
        return
    if not diff.strip():
        post.publish({"summary": "No code changes since the last review."}, ctx)
        return

    if os.environ.get("MOCK_REVIEW_FILE"):
        raw = json.loads(pathlib.Path(os.environ["MOCK_REVIEW_FILE"]).read_text())
    else:
        guide = pathlib.Path("REVIEW.md")
        guidelines = guide.read_text() if guide.exists() else "(no repo-specific guidelines)"
        open_prev = [{k: f[k] for k in ("id", "path", "line", "severity", "title")}
                     for f in (prior or {}).get("findings", []) if f.get("status") == "open"]
        prompt = build_prompt(guidelines, jira_md, os.environ.get("PR_TITLE", ""),
                              os.environ.get("PR_BODY", ""), open_prev, diff)
        system = (PROMPTS / "system.md").read_text()
        text = run_claude(prompt, system)
        try:
            raw = extract_json(text)
        except ValueError:  # one retry, asking for JSON only
            raw = extract_json(run_claude(prompt + "\n\nReturn valid JSON only.", system))

    result = validate(raw, parse_diff_lines(diff))
    post.publish(result, ctx)
    placed = sum(1 for f in result["new_findings"] if f["placed"])
    print(f"Done: {len(result['new_findings'])} new finding(s), {placed} inline.")


if __name__ == "__main__":
    main()
