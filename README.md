# ai-review

Reusable GitHub Actions workflow that reviews pull requests with Claude Code and posts the results back to the PR.
It reads the Jira ticket from the branch name or PR title, checks the diff against the ticket's requirements, and
never approves or merges.

## Layout

```
scripts/
  diff_utils.py     Jira key extraction; which diff lines accept inline comments
  ghapi.py          gh CLI wrapper; hidden state marker in the summary comment
  jira_context.py   Jira ticket -> .ai-context/jira.md (+ screenshots)
  review.py         entry point: diff -> Claude (read-only) -> validate -> post
  post.py           inline review, resolved-thread edits, summary comment, check run
prompts/system.md   reviewer instructions and the JSON output contract
.github/workflows/review.yml   the reusable workflow
examples/caller-workflow.yml   what each app repo needs
tests/              offline unit tests and fixtures
```

## Run the tests (no accounts needed)

```bash
python3 -m python3 -m unittest discover -s tests -v
```

## Dry run of the whole pipeline (no GitHub, no API calls)

Run these from inside the sample app repo, with the `feature/DEMO-12-user-search` branch checked out:

```bash
AR=/path/to/ai-review
HEAD=$(git rev-parse HEAD); BASE=$(git rev-parse main)

# 1. Build the Jira context from the saved fixture
JIRA_KEY=DEMO-12 JIRA_FIXTURE=$AR/tests/fixtures/jira_DEMO-12.json python3 $AR/scripts/jira_context.py

# 2. Review the saved diff with a saved model output, printing the GitHub calls instead of making them
DRY_RUN=1 REPO=me/sample PR_NUMBER=1 HEAD_SHA=$HEAD BASE_SHA=$BASE \
  DIFF_FILE=$AR/tests/fixtures/sample.diff MOCK_REVIEW_FILE=$AR/tests/fixtures/mock_review.json \
  python3 $AR/scripts/review.py
```

The output lists the inline review, the summary comment, and the check run, with the conclusion `failure`.

## Live setup (after you have a GitHub account and a Jira site)

1. Create a GitHub repo `ai-review` and push this folder. Make it public, or add `AI_REVIEW_REPO_TOKEN` (a PAT with read access to it).
2. In the sample app repo, set repository secrets:
   - `ANTHROPIC_API_KEY`: a key with a low spend limit
   - `JIRA_BASE_URL`: e.g. `https://yoursite.atlassian.net`
   - `JIRA_EMAIL`: your Atlassian account email
   - `JIRA_API_TOKEN`: from id.atlassian.com → Security → API tokens
3. Copy `examples/caller-workflow.yml` to `.github/workflows/ai-review.yml` in the app repo, replacing `YOUR_GITHUB_USERNAME`.
4. Open a PR from `feature/DEMO-12-user-search`. The check and comments appear after a minute or two.

## Design rules

- Claude runs with read-only tools (Read, Grep, Glob). Bash, Edit, Write, and web access are disabled.
- Reviews are posted as `COMMENT` only, so branch protection and human approval are unchanged.
- Jira text and PR text are treated as data, not instructions (see `prompts/system.md`).
- Inline comments go only on lines that are in the diff. Anything else is listed in the summary.
- Re-reviews look only at new commits, carry findings forward, and edit the existing summary instead of adding a new one.
