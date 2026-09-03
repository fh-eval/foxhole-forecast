#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || -z "$1" ]]; then
  echo "usage: $0 <commit-message>" >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target_branch="${PERSIST_TARGET_BRANCH:-main}"

git config user.name "${PERSIST_GIT_USER_NAME:-foxhole-forecast[bot]}"
git config user.email "${PERSIST_GIT_USER_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
git add -- data

if git diff --cached --quiet; then
  echo "No persisted changes"
  exit 0
fi

"$script_dir/assert-data-only.sh" --staged
git commit -m "$1"
git fetch origin "$target_branch"
git rebase "origin/$target_branch"
"$script_dir/assert-data-only.sh" "origin/$target_branch"
git push origin "HEAD:$target_branch"
