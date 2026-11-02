#!/bin/bash
# Save Part 9 (MoQ layer on picoquic) on GitHub as its own branch, on top of main (Parts 1-8):
#   part9-moq -> new "Part 9" commit (files below)
#   main      -> everything (same as part 9)
# Run it inside the repository folder in the VM:   bash push_part9.sh
cd "$(dirname "$0")" || exit 1

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "This folder is not a git repository."; exit 1
fi

if git rev-parse -q --verify part9-moq >/dev/null; then
  git checkout -q part9-moq                 # run again: add the new changes on top
else
  git checkout -q main && git checkout -q -b part9-moq   # first time: start from main (Parts 1-8)
fi

for f in .gitignore README.md COMMANDS.md PART9_MOQ.md moq_test.py push_part9.sh congestion_test.py PART8_CONDITIONS.md \
         moq/Makefile moq/README.md moq/moqt.h moq/moqt.c moq/moqpico.c \
         moq/moqtail_interop.sh moq/moqtail-0.14.1-ipv4.patch; do
  [ -e "$f" ] && git add -- "$f"
done
if git diff --cached --quiet; then
  echo "--- nothing new to commit for Part 9"
else
  git commit -q -m "Part 9: MoQ (MOQT draft-16, MoQtail version) layer on picoquic - video as a MoQ track, one group per GOP; I-frame objects on a subgroup stream of the better path's session, P/B objects as datagrams on the other; Part 8 controller decides the paths; delivery timeout for late groups and a congestion-window watchdog; works with the MoQtail relay 0.14.1; commands in PART9_MOQ.md" \
                && echo "+++ committed Part 9"
fi

git branch -f main HEAD
git checkout -q main

echo
git log --oneline --graph --decorate -7 main
echo
echo "Pushing part9-moq and main to $(git remote get-url origin) ..."
echo "When asked: Username = your GitHub username, Password = your personal access token"
if git push origin part9-moq main; then
  echo
  echo "Done - https://github.com/SSR7777/mininet-wifi-5g/tree/part9-moq"
else
  echo
  echo "Push was refused (wrong token, or GitHub has newer changes). Fix it and run again:"
  echo "    bash push_part9.sh"
fi
