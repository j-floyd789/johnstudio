#!/usr/bin/env bash
# Install (or reinstall) a launchd user agent that runs `johnstudio server`
# at login and respawns it on crash. Logs to ~/.johnstudio/server.{out,err}.log.
#
# Idempotent: bootstraps if not loaded, kickstarts if already loaded.
# Run with:  bash scripts/install_launch_agent.sh
# Uninstall: launchctl bootout gui/$(id -u)/com.johnstudio.server &&
#            rm ~/Library/LaunchAgents/com.johnstudio.server.plist
set -euo pipefail

LABEL="com.johnstudio.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HOME_DIR="${JOHNSTUDIO_HOME:-$HOME/.johnstudio}"

# Find the johnstudio CLI on PATH or in the common pip --user bin dir.
CLI="$(command -v johnstudio || true)"
if [ -z "$CLI" ]; then
  for c in \
    "$HOME/Library/Python/3.11/bin/johnstudio" \
    "$HOME/Library/Python/3.12/bin/johnstudio" \
    "$HOME/Library/Python/3.13/bin/johnstudio" \
    "/opt/homebrew/bin/johnstudio" \
    "/usr/local/bin/johnstudio"
  do
    if [ -x "$c" ]; then CLI="$c"; break; fi
  done
fi
if [ -z "$CLI" ]; then
  echo "error: johnstudio CLI not found. Run 'pip install -e .' first." >&2
  exit 1
fi

mkdir -p "$HOME_DIR" "$(dirname "$PLIST")"

# launchd starts with a minimal PATH that excludes nvm, Codex.app, and any
# other tool dir the user picked up in their shell rc. The backend's
# system-health check shells out to `claude`, `codex`, `gemini`, `tmux`,
# `git` — so without their directories on PATH every worker reports
# "missing" in the UI. Detect each tool's parent dir from the user's
# current shell and bake them into the plist.
collect_paths() {
  local -a dirs=()
  local p
  for tool in claude codex gemini tmux git node; do
    p="$(command -v "$tool" 2>/dev/null || true)"
    if [ -n "$p" ]; then
      dirs+=("$(dirname "$p")")
    fi
  done
  # Always include the standard system + brew dirs as a fallback.
  dirs+=("/opt/homebrew/bin" "/usr/local/bin" "/usr/bin" "/bin")
  # De-dup while preserving order.
  printf "%s\n" "${dirs[@]}" | awk '!seen[$0]++' | paste -sd: -
}
LAUNCH_PATH="$(collect_paths)"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$CLI</string>
        <string>server</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$LAUNCH_PATH</string>
        <key>JOHNSTUDIO_HOME</key>
        <string>$HOME_DIR</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>$HOME_DIR</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>$HOME_DIR/server.out.log</string>

    <key>StandardErrorPath</key>
    <string>$HOME_DIR/server.err.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF

DOMAIN="gui/$(id -u)"
TARGET="$DOMAIN/$LABEL"

# Re-bootstrap so plist edits take effect. `bootout` errors if not loaded;
# we tolerate that. The sleep gives launchd time to fully release the label
# before we bootstrap again — without it, bootstrap races with bootout and
# fails with "5: Input/output error".
if launchctl print "$TARGET" >/dev/null 2>&1; then
  launchctl bootout "$TARGET" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    launchctl print "$TARGET" >/dev/null 2>&1 || break
    sleep 0.5
  done
fi
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$TARGET" 2>/dev/null || true

echo "installed: $PLIST"
echo "logs:      $HOME_DIR/server.{out,err}.log"
echo "status:    launchctl print $TARGET"
echo "stop:      launchctl bootout $TARGET"
