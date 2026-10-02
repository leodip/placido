#!/usr/bin/env bash
# Rebuild the placido sandbox from the template: a fresh local repository with
# one commit on main, tagged `start`. Each issue placido is working on is first
# closed with `placido close --force`, so its run is ended and logged as closed;
# any other worktree is removed with its Herdr workspace. Branches go with the
# repository. The sandbox's own workspace stays open, since the user may be
# working in it.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
sandbox="${PLACIDO_SANDBOX:-$HOME/code/placido-sandbox}"
placido="$here/../bin/placido"

if [ -d "$sandbox/.git" ]; then
  git -C "$sandbox" worktree list --porcelain | sed -n 's/^worktree //p' | tail -n +2 |
    while read -r path; do
      if (cd "$path" && "$placido" close --force >/dev/null 2>&1); then
        echo "closed the placido run in $path"
      fi
    done
fi

if command -v herdr >/dev/null && herdr status server >/dev/null 2>&1; then
  herdr workspace list | python3 -c '
import json, sys
for w in json.load(sys.stdin)["result"]["workspaces"]:
    tree = w.get("worktree") or {}
    if tree.get("is_linked_worktree") and tree.get("repo_root") == sys.argv[1]:
        print(w["workspace_id"], w["label"])
' "$sandbox" | while read -r id label; do
    herdr workspace close "$id" >/dev/null
    echo "closed Herdr workspace $id ($label)"
  done
fi

if [ -d "$sandbox/.git" ]; then
  git -C "$sandbox" worktree list --porcelain | sed -n 's/^worktree //p' | tail -n +2 |
    while read -r path; do
      git -C "$sandbox" worktree remove --force "$path"
      echo "removed worktree $path"
    done
fi
rm -rf "$sandbox"
mkdir -p "$sandbox"
cp -R "$here/template/." "$sandbox/"
cd "$sandbox"
git init -q -b main
git add -A
# A fixed author and date give the starting commit the same hash on every reset.
GIT_AUTHOR_NAME=placido GIT_AUTHOR_EMAIL=placido@localhost \
GIT_COMMITTER_NAME=placido GIT_COMMITTER_EMAIL=placido@localhost \
GIT_AUTHOR_DATE="2026-10-02T00:00:00Z" GIT_COMMITTER_DATE="2026-10-02T00:00:00Z" \
  git commit -q -m "Start the placido sandbox"
git tag start
echo "sandbox ready at $sandbox ($(git rev-parse --short HEAD))"
