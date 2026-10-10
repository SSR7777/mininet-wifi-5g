#!/bin/bash
# Remove the "Co-Authored-By: Claude ..." and "Claude-Session: ..." lines from every
# commit message on the project branches, then force-push the branches to GitHub.
# The files themselves do not change - only the commit messages.
#
# Run it inside the repository folder in the VM:   bash remove_coauthor.sh
cd "$(dirname "$0")" || exit 1
BRANCHES="part1-wifi-5g-comparison part2-video-streaming part3-picoquic part4-multipath-live-video part5-iframe-aware main"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "This folder is not a git repository."; exit 1
fi
for b in $BRANCHES; do
  git rev-parse -q --verify "$b" >/dev/null || { echo "Branch $b not found."; exit 1; }
done

# 1. the push scripts no longer add the co-author line: commit them first
git checkout -q part5-iframe-aware
for f in push_to_github.sh organize_github.sh push_part5.sh remove_coauthor.sh; do
  [ -e "$f" ] && git add -- "$f"
done
if ! git diff --cached --quiet; then
  git commit -q -m "Push scripts: no co-author line in commit messages" && echo "+++ committed the updated scripts"
fi
git branch -f main part5-iframe-aware
git checkout -q main

# 2. safety copy of everything (restore with: git clone ../mininet-wifi-5g-backup.bundle)
git bundle create ../mininet-wifi-5g-backup.bundle --all >/dev/null 2>&1 && \
  echo "*** Backup saved: $(cd .. && pwd)/mininet-wifi-5g-backup.bundle"

# 3. rewrite the commit messages on all branches (same commits, same files)
echo "*** Removing the co-author lines from every commit message ..."
export FILTER_BRANCH_SQUELCH_WARNING=1
stashed=0
if ! git diff --quiet || ! git diff --cached --quiet; then      # other changed files: keep them aside
  git stash -q && stashed=1
fi
git filter-branch -f --msg-filter \
  "sed -e '/^Co-Authored-By: Claude/d' -e '/^Claude-Session:/d' | awk 'NF{for(;n>0;n--)print \"\";print;next}{n++}'" \
  -- $BRANCHES >/dev/null || { echo "Rewrite failed - nothing was pushed."; exit 1; }
rm -rf .git/refs/original
[ "$stashed" = 1 ] && git stash pop -q

left=$(git log --format=%B $BRANCHES | grep -c -E '^(Co-Authored-By: Claude|Claude-Session:)')
echo "*** Co-author lines left: $left"
echo
git log --oneline --graph --decorate -8 main
echo

# 4. replace the branches on GitHub
echo "Pushing all branches (force) to $(git remote get-url origin) ..."
echo "When asked: Username = your GitHub username, Password = your personal access token"
if git push --force origin $BRANCHES; then
  echo
  echo "Done. GitHub can take a little while to update the Contributors box."
  echo "Delete the token now: https://github.com/settings/tokens"
else
  echo
  echo "Push was refused (wrong token?). The commits are already cleaned here - just run:"
  echo "    git push --force origin $BRANCHES"
fi
