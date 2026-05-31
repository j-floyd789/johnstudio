#!/usr/bin/env bash
# Start backend + UI together. Uses tmux if available; otherwise foregrounds backend
# and launches UI in a child process.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

if command -v tmux >/dev/null 2>&1; then
  SESSION="johnstudio-dev"
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux kill-session -t "$SESSION"
  fi
  tmux new-session -d -s "$SESSION" -c "$HERE/.."
  tmux send-keys -t "$SESSION:0" "bash $HERE/start_backend.sh" C-m
  tmux split-window -t "$SESSION:0" -h -c "$HERE/.."
  tmux send-keys -t "$SESSION:0.1" "bash $HERE/start_ui.sh" C-m
  echo "Started in tmux session: $SESSION"
  echo "Attach with:  tmux attach -t $SESSION"
  exit 0
fi

echo "tmux not found — running backend in background, UI in foreground."
bash "$HERE/start_backend.sh" &
BACKEND_PID=$!
trap "kill $BACKEND_PID 2>/dev/null || true" EXIT
sleep 1
bash "$HERE/start_ui.sh"
