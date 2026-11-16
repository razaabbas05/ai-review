#!/usr/bin/env bash
# Publish both sample repos to your GitHub account.
# Usage: scripts/publish_to_github.sh <your-github-username> <path-to-sample-app>
# Needs: gh (logged in with `gh auth login`) and git.
set -euo pipefail

GH_USER="${1:?usage: publish_to_github.sh <github-username> <sample-app-dir>}"
SAMPLE_DIR="${2:?usage: publish_to_github.sh <github-username> <sample-app-dir>}"
AR_DIR="$(cd "$(dirname "$0")/.." && pwd)"

gh auth status >/dev/null 2>&1 || { echo "Not logged in. Run: gh auth login"; exit 1; }

# 1. Reviewer repo: public, so the reusable workflow can be called from any repo you own.
cd "$AR_DIR"
[ -d .git ] || git init -q -b main
git add -A
git diff --cached --quiet || git commit -qm "ai-review: initial version"
gh repo create "$GH_USER/ai-review" --public --source=. --remote=origin --push

# 2. Sample app: point its caller workflow at your reviewer repo on every branch, then push all branches.
cd "$SAMPLE_DIR"
for b in $(git for-each-ref --format='%(refname:short)' refs/heads); do
  git checkout -q "$b"
  if grep -q YOUR_GITHUB_USERNAME .github/workflows/ai-review.yml; then
    sed -i "s/YOUR_GITHUB_USERNAME/$GH_USER/g" .github/workflows/ai-review.yml
    git commit -qam "Point AI review at $GH_USER/ai-review"
  fi
done
git checkout -q main
gh repo create "$GH_USER/ai-review-sample" --private --source=. --remote=origin --push
git push -q origin --all

echo
echo "Done. Next: add the secrets to $GH_USER/ai-review-sample (see README, 'Live setup'),"
echo "then open a PR from feature/DEMO-12-user-search into main."
