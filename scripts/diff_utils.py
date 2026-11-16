"""Helpers: find Jira keys, and find which lines GitHub will accept inline comments on."""
import re

KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def extract_jira_key(*texts):
    """Return the first Jira-style key (e.g. DEMO-12) found in the given texts, or ''."""
    for text in texts:
        found = KEY_RE.findall(text or "")
        if found:
            return found[0]
    return ""


def parse_diff_lines(diff):
    """Map each file path to the new-file line numbers shown in the diff (added or context lines).

    GitHub rejects inline comments on any other line, so the reviewer's findings are checked against this.
    """
    valid = {}
    path = None
    new_line = None
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            path, new_line = None, None
            continue
        # The '+++ b/...' file header only appears outside a hunk; inside one it is ordinary added content.
        if line.startswith("+++ ") and new_line is None:
            target = line[4:].strip()
            path = target[2:] if target.startswith("b/") else None
            continue
        m = HUNK_RE.match(line)
        if m:
            new_line = int(m.group(1))
            continue
        if path is None or new_line is None:
            continue
        if line.startswith("+") or line.startswith(" "):
            valid.setdefault(path, set()).add(new_line)
            new_line += 1
        # '-' lines and '\ No newline' markers do not move the new-file counter.
    return valid
