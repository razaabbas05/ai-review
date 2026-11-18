"""Stateful failure/retry tests; no external services are called."""
import copy
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import post
import review
from ghapi import find_state


def result():
    return {"summary": "Search reviewed", "requirements": [
        {"id": "R1", "text": "Admins only", "status": "not_met", "evidence": "users.py:2"}],
        "new_findings": [{"path": "users.py", "line": 2, "title": "Missing authorization",
                          "body": "Check the role", "severity": "High", "category": "security", "placed": True}],
        "previous_findings": []}


class FakeGitHub:
    def __init__(self):
        self.summary = None
        self.comments = []
        self.checks = []
        self.reviews = 0
        self.fail = None

    def state(self):
        return find_state([self.summary] if self.summary else [])

    def api(self, path, method="GET", payload=None, paginate=False):
        if path.endswith("/pulls/1/comments"):
            return copy.deepcopy(self.comments)
        if method == "POST" and path.endswith("/reviews"):
            self.reviews += 1
            for c in payload["comments"]:
                self.comments.append({**c, "id": len(self.comments) + 100,
                                      "original_line": c["line"], "commit_id": payload["commit_id"],
                                      "pull_request_review_id": self.reviews,
                                      "user": {"login": "github-actions[bot]"}})
            if self.fail == "review_response":
                self.fail = None
                raise RuntimeError("Response lost after GitHub accepted review")
            return {"id": self.reviews}
        if "/issues/" in path:
            if self.fail == "final_summary" and '"pending":' not in payload["body"]:
                self.fail = None
                raise RuntimeError("Summary update failed")
            self.summary = {"id": 42, "body": payload["body"], "user": {"login": "github-actions[bot]"}}
            return {"id": 42}
        if path.endswith("/check-runs"):
            self.checks.append(payload)
        return {}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.ctx = {"repo": "me/sample", "pr": "1", "head_sha": "head", "range": "base..head",
                    "mode": "full", "summary_id": None, "prior": None, "limit": 3000,
                    "diff_lines": 10, "too_large": False, "jira_note": ""}
        for target, value in (("DRY_RUN", False), ("gh_api", self.github.api)):
            p = patch.object(post, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_skipped_review_never_advances_checkpoint(self):
        for prior in (None, {"last_sha": "old", "findings": []}):
            with self.subTest(prior=prior):
                post.publish({}, {**self.ctx, "prior": prior, "too_large": True})
                state = self.github.state()[1]
                self.assertEqual(state["last_sha"], (prior or {}).get("last_sha"))
                self.assertEqual(self.github.checks[-1]["conclusion"], "neutral")

    def test_unchanged_rerun_keeps_requirements_and_findings(self):
        post.publish(result(), self.ctx)
        cid, prior = self.github.state()
        post.publish(prior["result"], {**self.ctx, "summary_id": cid, "prior": prior, "unchanged": True})
        state = self.github.state()[1]
        self.assertEqual(state["findings"], prior["findings"])
        self.assertEqual(state["result"], prior["result"])
        self.assertIn("Admins only", self.github.summary["body"])
        self.assertEqual(self.github.reviews, 1)
        self.assertEqual(self.github.checks[-1]["conclusion"], "failure")

    def test_retry_after_partial_publication_does_not_duplicate(self):
        for failure in ("review_response", "final_summary"):
            with self.subTest(failure=failure):
                self.github.summary = None
                self.github.comments = []
                self.github.reviews = 0
                self.github.fail = failure
                with self.assertRaises(RuntimeError):
                    post.publish(result(), self.ctx)
                cid, prior = self.github.state()
                self.assertNotIn("last_sha", prior)
                pending = prior["pending"]
                post.publish(pending["result"], {**pending["ctx"], "summary_id": cid, "prior": prior})
                state = self.github.state()[1]
                self.assertNotIn("pending", state)
                self.assertEqual(state["last_sha"], "head")
                self.assertEqual(self.github.reviews, 1)
                self.assertEqual(len(self.github.comments), 1)
                self.assertEqual(state["findings"][0]["comment_id"], 100)

    def test_main_retries_invalid_output_then_reports_incomplete(self):
        with patch.object(review, "load_prior", return_value=(None, {"last_sha": "old", "findings": []})), \
                patch.object(review, "compute_diff", return_value=("+code", "incremental", "old..head")), \
                patch.object(review, "run_claude", return_value="{}") as model, \
                patch.dict("os.environ", {"REPO": "me/sample", "PR_NUMBER": "1", "HEAD_SHA": "head"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                review.main()
        self.assertEqual(model.call_count, 2)
        self.assertEqual(self.github.state()[1]["last_sha"], "old")
        self.assertEqual(self.github.checks[-1]["conclusion"], "failure")
        self.assertIn("incomplete", self.github.checks[-1]["output"]["title"])
        self.assertEqual(self.github.reviews, 0)

    def test_invalid_shapes_and_entries_are_rejected(self):
        invalid = [{}, [], None, {**result(), "new_findings": None},
                   {**result(), "new_findings": [{}]},
                   {**result(), "new_findings": [{**result()["new_findings"][0], "line": True}]},
                   {**result(), "requirements": [None]},
                   {**result(), "previous_findings": [{"id": "F-1", "status": "maybe"}]}]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                review.validate(raw, {})

    def test_main_resumes_saved_review_without_calling_model(self):
        self.github.fail = "review_response"
        with self.assertRaises(RuntimeError):
            post.publish(result(), self.ctx)
        with patch.object(review, "load_prior", side_effect=lambda *_: self.github.state()), \
                patch.object(review, "compute_diff", return_value=("", "incremental", "head..head")), \
                patch.object(review, "run_claude") as model, \
                patch.dict("os.environ", {"REPO": "me/sample", "PR_NUMBER": "1", "HEAD_SHA": "head"}, clear=True):
            review.main()
        model.assert_not_called()
        self.assertEqual(self.github.reviews, 1)
        self.assertNotIn("pending", self.github.state()[1])
        self.assertIn("Admins only", self.github.summary["body"])

    def test_legacy_summary_is_retained_on_unchanged_run(self):
        prior = {"last_sha": "head", "findings": [],
                 "legacy_summary": "## AI review\n\n| R1 | Admins only | not met |"}
        post.publish({}, {**self.ctx, "prior": prior, "unchanged": True})
        self.assertIn("| R1 | Admins only | not met |", self.github.summary["body"])
        self.assertNotIn("No requirements to check", self.github.summary["body"])

    def test_valid_clean_review_can_succeed(self):
        clean = {"summary": "No issues found", "requirements": [], "new_findings": [], "previous_findings": []}
        post.publish(review.validate(clean, {}), self.ctx)
        self.assertEqual(self.github.checks[-1]["conclusion"], "success")
        self.assertEqual(self.github.state()[1]["last_sha"], "head")


if __name__ == "__main__":
    unittest.main()
