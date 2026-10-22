#!/bin/bash
# Save Part 6 (adaptive path switching) on GitHub as its own branch, on top of Part 5:
#   part6-adaptive-switching -> new "Part 6" commit (files below)
#   main                     -> everything (same as part 6)
# Run it inside the repository folder in the VM:   bash push_part6.sh
cd "$(dirname "$0")" || exit 1

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "This folder is not a git repository."; exit 1
fi
if ! git rev-parse -q --verify part5-iframe-aware >/dev/null; then
  echo "Branch part5-iframe-aware not found - run push_part5.sh first."; exit 1
fi

# Part 6 starts from Part 5 (keeps the changed files in the folder)
if git rev-parse -q --verify part6-adaptive-switching >/dev/null; then
  git checkout -q part6-adaptive-switching               # run again: add the new changes on top
else
  git checkout -q -b part6-adaptive-switching            # first time: start from the current state (Part 5)
fi

for f in .gitignore adaptive_test.py config.ini README.md GUIDE.md PART6_ADAPTIVE.md COMMANDS.md \
         push_part6.sh; do
  [ -e "$f" ] && git add -- "$f"
done
if git diff --cached --quiet; then
  echo "--- nothing new to commit for Part 6"
else
  git commit -q -m "Part 6: adaptive path switching for a long video - capacity of 5G and Wi-Fi changes over time, the sender probes both paths before every 20 s segment and sends I frames as a QUIC stream on the better path and P/B frames as datagrams on the other; compared with static (Part 5) and oracle switching; step-by-step commands in PART6_ADAPTIVE.md" \
                && echo "+++ committed Part 6"
fi

git branch -f main HEAD
git checkout -q main

echo
git log --oneline --graph --decorate -7 main
echo
echo "Pushing part6-adaptive-switching and main to $(git remote get-url origin) ..."
echo "When asked: Username = your GitHub username, Password = your personal access token"
if git push origin part6-adaptive-switching main; then
  echo
  echo "Done - https://github.com/SSR7777/mininet-wifi-5g/tree/part6-adaptive-switching"
else
  echo
  echo "Push was refused (wrong token, or GitHub has newer changes). Fix it and run again:"
  echo "    bash push_part6.sh"
fi
