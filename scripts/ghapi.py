"""Thin wrapper around the gh CLI, plus the hidden state marker kept in the bot's summary comment."""
import json
import re
import subprocess

STATE_RE = re.compile(r"<!-- ai-review-state: (.*?) -->", re.S)


def gh_api(path, method="GET", payload=None, paginate=False):
    cmd = ["gh", "api", "-X", method, path]
    if paginate:
        cmd.append("--paginate")
    stdin = None
    if payload is not None:
        cmd += ["--input", "-"]
        stdin = json.dumps(payload)
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {method} {path} failed: {proc.stderr.strip()}")
    text = proc.stdout.strip()
    if not text:
        return None
    return _decode_pages(text) if paginate else json.loads(text)


def _decode_pages(text):
    """--paginate prints one JSON array per page, back to back. Join them into one list."""
    decoder = json.JSONDecoder()
    items, idx = [], 0
    while idx < len(text):
        obj, idx = decoder.raw_decode(text, idx)
        items.extend(obj if isinstance(obj, list) else [obj])
        while idx < len(text) and text[idx].isspace():
            idx += 1
    return items


def render_state(state):
    body = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
    # Keep the marker from ending early if a title contains "-->".
    return "<!-- ai-review-state: " + body.replace("-->", "--\\u003e") + " -->"


def find_state(comments):
    """Return (comment_id, state) from the bot's own summary comment, or (None, None)."""
    for c in comments or []:
        login = (c.get("user") or {}).get("login", "")
        m = STATE_RE.search(c.get("body") or "")
        if m and login.endswith("[bot]"):
            return c["id"], json.loads(m.group(1))
    return None, None
