#!/bin/bash
# Save Part 5 (I-frame aware sending) on GitHub as its own branch, on top of Part 4:
#   part5-iframe-aware -> new "Part 5" commit (files below)
#   main               -> everything (same as part 5)
# Run it inside the repository folder in the VM:   bash push_part5.sh
cd "$(dirname "$0")" || exit 1

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "This folder is not a git repository."; exit 1
fi
if ! git rev-parse -q --verify part4-multipath-live-video >/dev/null; then
  echo "Branch part4-multipath-live-video not found - run organize_github.sh first."; exit 1
fi

TRAILER="Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Q26dUmyqp58jnhPvfo83ko"

# Part 5 starts from Part 4 (keeps the changed files in the folder)
if git rev-parse -q --verify part5-iframe-aware >/dev/null; then
  git checkout -q part5-iframe-aware               # run again: add the new changes on top
else
  git checkout -q -b part5-iframe-aware            # first time: start from the current state (Part 4)
fi

for f in .gitignore iframe_test.py players_test.py show_results.py README.md GUIDE.md \
         PART5_IFRAME.md COMMANDS.md push_part5.sh; do
  [ -e "$f" ] && git add -- "$f"
done
if git diff --cached --quiet; then
  echo "--- nothing new to commit for Part 5"
else
  git commit -q -m "Part 5: I-frame aware sending - I/P/B frame analysis (H.264, HEVC), effect of one lost frame, I frames on 5G QUIC streams and P/B frames on Wi-Fi datagrams (Wi-Fi only, 5G only, Wi-Fi + 5G); step-by-step commands in PART5_IFRAME.md" \
                -m "$TRAILER" && echo "+++ committed Part 5"
fi

git branch -f main HEAD
git checkout -q main

echo
git log --oneline --graph --decorate -7 main
echo
echo "Pushing part5-iframe-aware and main to $(git remote get-url origin) ..."
echo "When asked: Username = your GitHub username, Password = your personal access token"
if git push origin part5-iframe-aware main; then
  echo
  echo "Done - https://github.com/SSR7777/mininet-wifi-5g/tree/part5-iframe-aware"
else
  echo
  echo "Push was refused (wrong token, or GitHub has newer changes). Fix it and run again:"
  echo "    bash push_part5.sh"
fi
