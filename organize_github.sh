#!/bin/bash
# Organize the project on GitHub as one branch per part:
#   part1-wifi-5g-comparison   -> the "Part 1" commit
#   part2-video-streaming      -> the "Part 2" commit
#   part3-picoquic             -> the "Part 3" commit
#   part4-multipath-live-video -> a new "Part 4" commit with the new work
#   main                       -> everything (same as part 4)
# Run it inside the repository folder in the VM:   bash organize_github.sh
cd "$(dirname "$0")" || exit 1

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "This folder is not a git repository."; exit 1
fi
git config user.name  >/dev/null || { read -rp "Your name for git commits: " n;  git config --global user.name "$n"; }
git config user.email >/dev/null || { read -rp "Your email for git commits: " e; git config --global user.email "$e"; }

TRAILER="Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Q26dUmyqp58jnhPvfo83ko"

find_part () { git log --format=%H -n1 --grep="^Part $1:" HEAD; }
P1=$(find_part 1); P2=$(find_part 2); P3=$(find_part 3)
if [ -z "$P1" ] || [ -z "$P2" ] || [ -z "$P3" ]; then
  echo "Could not find the 'Part 1/2/3' commits (run push_to_github.sh first)."; exit 1
fi
echo "Part 1: $(git log --format='%h %s' -n1 "$P1")"
echo "Part 2: $(git log --format='%h %s' -n1 "$P2")"
echo "Part 3: $(git log --format='%h %s' -n1 "$P3")"

# Branches for parts 1-3 (they point at the existing commits)
git branch -f part1-wifi-5g-comparison "$P1"
git branch -f part2-video-streaming    "$P2"
git branch -f part3-picoquic           "$P3"

# Part 4: commit the new work on its own branch (keeps the changed files)
git checkout -q -B part4-multipath-live-video
for f in .gitignore players_test.py show_results.py README.md GUIDE.md install_picoquic.sh \
         patch_quicperf_video.py make_grid.py push_to_github.sh organize_github.sh; do
  [ -e "$f" ] && git add -- "$f"
done
if git diff --cached --quiet; then
  echo "--- nothing new to commit for Part 4"
else
  git commit -q -m "Part 4: Wi-Fi + 5G together (multipath QUIC, MPTCP) and the real video sent live over picoquic, grid video for VLC; step-by-step guide" \
                -m "$TRAILER" && echo "+++ committed Part 4"
fi

# main = everything
git branch -f main HEAD
git checkout -q main

echo
git log --oneline --graph --decorate -6 main part1-wifi-5g-comparison
echo
echo "Pushing all branches to $(git remote get-url origin) ..."
echo "When asked: Username = your GitHub username, Password = your personal access token"
if git push origin part1-wifi-5g-comparison part2-video-streaming part3-picoquic \
                   part4-multipath-live-video main; then
  echo
  echo "Done - branches: https://github.com/SSR7777/mininet-wifi-5g/branches"
  echo "Delete the token on GitHub now (Settings - Developer settings - Personal access tokens)."
else
  echo
  echo "Push was refused. If GitHub has newer changes on main, run:"
  echo "    git pull --rebase origin main     and then    bash organize_github.sh"
fi
