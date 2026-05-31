#!/usr/bin/env bash
# Set up a temp JohnStudio home + a demo git repo, then print instructions for the UI.
# This script does NOT start servers; run scripts/dev_all.sh separately so you can see
# each process's logs.
set -euo pipefail

HOME_DIR="${JOHNSTUDIO_DEMO_HOME:-/tmp/johnstudio-demo}"
REPO_DIR="${JOHNSTUDIO_DEMO_REPO:-/tmp/johnstudio-demo-repo}"

export JOHNSTUDIO_HOME="$HOME_DIR"

echo "==> JOHNSTUDIO_HOME=$JOHNSTUDIO_HOME"
echo "==> demo repo: $REPO_DIR"

rm -rf "$HOME_DIR" "$REPO_DIR"

mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
git config user.email demo@johnstudio.local
git config user.name demo
echo "# Demo" > README.md
echo '{}' > package.json
echo '' > tsconfig.json
echo '' > next.config.ts
git add -A
git commit -qm "demo seed"
cd - >/dev/null

echo "==> johnstudio init (auto-imports seed skills)"
johnstudio init >/dev/null

echo "==> johnstudio add-project demo $REPO_DIR"
johnstudio add-project demo "$REPO_DIR" >/dev/null

cat <<EOF

Demo ready.

In one terminal:
  export JOHNSTUDIO_HOME=$HOME_DIR
  bash scripts/start_backend.sh

In another:
  bash scripts/start_ui.sh

Then open:
  http://localhost:5173

Try:
  1) Select the "demo" project.
  2) Type a task: "add a hello endpoint"
  3) Leave "Stub-only" on (offline-safe).
  4) Click Run.
  5) On the task page: collect, review, then Merge.
EOF
