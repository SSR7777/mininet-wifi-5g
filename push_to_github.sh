#!/bin/bash
# Push the project to GitHub as three commits, in order:
#   Part 1 - Wi-Fi vs 5G comparison
#   Part 2 - video streaming, live and not live
#   Part 3 - QUIC with picoquic
# Run it inside the repository folder:   bash push_to_github.sh
cd "$(dirname "$0")" || exit 1

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "This folder is not a git repository. Run:  git clone https://github.com/SSR7777/mininet-wifi-5g.git"
  exit 1
fi
git config user.name  >/dev/null || { read -rp "Your name for git commits: " n;  git config --global user.name "$n"; }
git config user.email >/dev/null || { read -rp "Your email for git commits: " e; git config --global user.email "$e"; }

TRAILER="Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Q26dUmyqp58jnhPvfo83ko"

commit_part () {            # $1 = message, rest = files
  msg="$1"; shift
  git add -- "$@"
  if git diff --cached --quiet; then
    echo "--- nothing new for: $msg"
  else
    git commit -q -m "$msg" -m "$TRAILER" && echo "+++ committed: $msg"
  fi
}

commit_part "Part 1: Wi-Fi vs 5G comparison (config file, topology, background traffic, sweeps)" \
  .gitignore install.sh config.ini netconfig.py wifi_5g_topology.py sweep.py plot.py vary_traffic.py

commit_part "Part 2: video streaming to the receiver, delay measurements, live and not-live video players" \
  video_stream.py players_test.py show_results.py

commit_part "Part 3: QUIC with picoquic (file transfer and live streaming); README organised in three parts" \
  install_picoquic.sh quic_test.py quic_live.py README.md push_to_github.sh

echo
git log --oneline -5
echo
echo "Pushing to $(git remote get-url origin) ..."
echo "When asked: Username = your GitHub username, Password = your GitHub personal access token"
if git push origin HEAD:main; then
  echo "Done - open https://github.com/SSR7777/mininet-wifi-5g"
else
  echo
  echo "Push was refused. If GitHub has newer changes, run:"
  echo "    git pull --rebase origin main     and then    git push origin HEAD:main"
fi
