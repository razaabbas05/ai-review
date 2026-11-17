"""Offline tests: no GitHub, Jira, or Anthropic calls. Run with: python3 -m unittest discover -s tests -v"""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
FIX = ROOT / "tests" / "fixtures"

from diff_utils import extract_jira_key, parse_diff_lines  # noqa: E402
from ghapi import _decode_pages, find_state, render_state  # noqa: E402
import jira_context  # noqa: E402
import post  # noqa: E402
import review  # noqa: E402

post.DRY_RUN = True  # tests must never touch GitHub


class KeyTests(unittest.TestCase):
    def test_key_from_branch(self):
        self.assertEqual(extract_jira_key("feature/DEMO-12-user-search", ""), "DEMO-12")

    def test_key_falls_back_to_title(self):
        self.assertEqual(extract_jira_key("chore/tidy", "DEMO-7: fix login"), "DEMO-7")

    def test_no_key(self):
        self.assertEqual(extract_jira_key("main", "tidy up"), "")

    def test_lowercase_key_is_ignored(self):
        self.assertEqual(extract_jira_key("feature/demo-12"), "")


class DiffTests(unittest.TestCase):
    def test_added_and_context_lines_are_valid_removed_are_not(self):
        diff = "\n".join([
            "diff --git a/app/users.py b/app/users.py",
            "--- a/app/users.py",
            "+++ b/app/users.py",
            "@@ -1,3 +1,4 @@",
            " import sqlite3",
            "+import re",
            " ",
            "-old",
            "+new",
            "",
        ])
        self.assertEqual(parse_diff_lines(diff), {"app/users.py": {1, 2, 3, 4}})

    def test_plus_plus_plus_inside_a_hunk_is_content(self):
        diff = "\n".join([
            "diff --git a/a.txt b/a.txt",
            "--- a/a.txt",
            "+++ b/a.txt",
            "@@ -1,1 +1,2 @@",
            " a",
            "+++ looks like a header",
            "",
        ])
        self.assertEqual(parse_diff_lines(diff), {"a.txt": {1, 2}})

    def test_sample_fixture_has_expected_lines(self):
        valid = parse_diff_lines((FIX / "sample.diff").read_text())
        self.assertIn(19, valid["app/users.py"])   # the SQL line
        self.assertNotIn(99, valid["app/users.py"])


class ParseTests(unittest.TestCase):
    def test_extract_json_strips_fence_and_prose(self):
        text = 'Here you go:\n```json\n{"summary": "ok", "requirements": []}\n```'
        self.assertEqual(review.extract_json(text)["summary"], "ok")

    def test_validate_normalizes_and_marks_placement(self):
        raw = {
            "new_findings": [
                {"path": "app/users.py", "line": 2, "title": "t", "body": "b",
                 "severity": "Bogus", "category": "bug"},
                {"path": "app/users.py", "line": 99, "title": "t2", "body": "b",
                 "severity": "High", "category": "security"},
                {"path": "app/users.py", "title": "no line", "body": "b",
                 "severity": "High", "category": "bug"},
            ],
            "requirements": [{"id": "R1", "text": "x", "status": "maybe"}],
        }
        out = review.validate(raw, {"app/users.py": {1, 2, 3, 4}})
        self.assertEqual(len(out["new_findings"]), 2)           # the line-less finding is dropped
        self.assertEqual(out["new_findings"][0]["severity"], "Low")
        self.assertTrue(out["new_findings"][0]["placed"])
        self.assertFalse(out["new_findings"][1]["placed"])      # line 99 is outside the diff
        self.assertEqual(out["requirements"][0]["status"], "cant_verify")


class ClaudeCommandTests(unittest.TestCase):
    def model_used(self, env):
        with patch.dict("os.environ", env, clear=True):
            with patch("review.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = json.dumps({"result": "ok"})
                review.run_claude("prompt", "system")
                command = run.call_args.args[0]
        return command[command.index("--model") + 1]

    def test_defaults_to_haiku_5_5(self):
        self.assertEqual(self.model_used({}), "claude-haiku-5-5")

    def test_model_can_be_overridden(self):
        self.assertEqual(self.model_used({"CLAUDE_MODEL": "claude-sonnet-5-5"}),
                         "claude-sonnet-5-5")


class StateTests(unittest.TestCase):
    def test_bot_state_round_trip_ignores_humans(self):
        state = {"last_sha": "abc", "findings": [{"id": "F-1", "title": "x --> y"}]}
        comments = [
            {"id": 5, "user": {"login": "someone"}, "body": render_state(state)},
            {"id": 9, "user": {"login": "github-actions[bot]"},
             "body": "## AI review\n" + render_state(state)},
        ]
        cid, got = find_state(comments)
        self.assertEqual(cid, 9)
        self.assertEqual(got, state)

    def test_paginated_arrays_are_joined(self):
        self.assertEqual(_decode_pages('[{"id":1}][{"id":2}]\n'), [{"id": 1}, {"id": 2}])


class JiraTests(unittest.TestCase):
    def test_render_fixture(self):
        issue = json.loads((FIX / "jira_DEMO-12.json").read_text())
        md = jira_context.render("DEMO-12", issue, pathlib.Path(tempfile.mkdtemp()), live=False)
        self.assertIn("## Comments", md)
        self.assertIn("DEMO-7", md)
        self.assertIn("not downloaded", md)
        self.assertIn("Priya (PM)", md)


class PublishTests(unittest.TestCase):
    def ctx(self, **extra):
        base = {"repo": "me/sample", "pr": "1", "head_sha": "a" * 40, "range": "x..y", "mode": "full",
                "summary_id": None, "prior": None, "limit": 3000, "diff_lines": 10,
                "too_large": False, "jira_note": ""}
        base.update(extra)
        return base

    def run_publish(self, result, ctx):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            post.publish(result, ctx)
        return buf.getvalue()

    def test_first_run_posts_review_summary_and_check(self):
        result = {"summary": "s",
                  "requirements": [{"id": "R1", "text": "t", "status": "not_met", "evidence": "e"}],
                  "new_findings": [{"path": "app/users.py", "line": 2, "title": "T", "body": "B",
                                    "severity": "High", "category": "security", "placed": True}],
                  "previous_findings": []}
        out = self.run_publish(result, self.ctx())
        self.assertIn("POST repos/me/sample/pulls/1/reviews", out)
        self.assertIn("POST repos/me/sample/issues/1/comments", out)
        self.assertIn("POST repos/me/sample/check-runs", out)
        self.assertIn('"conclusion": "failure"', out)

    def test_resolved_finding_is_edited_not_reposted(self):
        prior = {"last_sha": "b" * 40, "findings": [
            {"id": "F-1", "path": "app/users.py", "line": 2, "severity": "High", "category": "bug",
             "title": "T", "status": "open", "comment_id": 7, "inline": True}]}
        result = {"summary": "fixed", "requirements": [], "new_findings": [],
                  "previous_findings": [{"id": "F-1", "status": "resolved"}]}
        out = self.run_publish(result, self.ctx(prior=prior, summary_id=42))
        self.assertIn("PATCH repos/me/sample/pulls/comments/7", out)
        self.assertIn("PATCH repos/me/sample/issues/comments/42", out)
        self.assertNotIn("/reviews", out)
        self.assertIn('"conclusion": "success"', out)

    def test_missing_comment_line_does_not_abort_publishing(self):
        result = {"summary": "s", "requirements": [],
                  "new_findings": [{"path": "app/users.py", "line": 2, "title": "T", "body": "B",
                                    "severity": "High", "category": "security", "placed": True}],
                  "previous_findings": []}
        calls = []

        def fake_gh_api(path, method="GET", payload=None, paginate=False):
            calls.append((path, method, payload, paginate))
            if path == "repos/me/sample/pulls/1/reviews":
                return {"id": 77}
            if path == "repos/me/sample/pulls/1/reviews/77/comments":
                return [
                    {"id": 123, "path": "app/users.py", "original_line": 2},
                    {"id": 124, "path": "app/users.py"},
                ]
            return {}

        with patch.object(post, "DRY_RUN", False), patch.object(post, "gh_api", side_effect=fake_gh_api):
            out = self.run_publish(result, self.ctx())

        self.assertIn("without a usable path, line, or ID", out)
        summary_call = next(c for c in calls if c[0] == "repos/me/sample/issues/1/comments")
        self.assertIn('"comment_id":123', summary_call[2]["body"])
        self.assertTrue(any(c[0] == "repos/me/sample/check-runs" for c in calls))


if __name__ == "__main__":
    unittest.main()
