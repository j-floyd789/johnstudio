#!/usr/bin/env bash
# Start the JohnStudio React UI in dev mode.
#
# Two preflight steps before launching vite:
#
# 1. Pick a node binary with the `disable-library-validation` entitlement.
#    Codex.app bundles a hardened node that refuses to load the rollup
#    .node binary on macOS — the dlopen fails with a Team-ID mismatch.
#    nvm-installed nodes are signed with that entitlement and work. We
#    source nvm.sh (if present) and `nvm use` the latest installed.
#
# 2. Read the loopback bearer token from $JOHNSTUDIO_HOME/server_token
#    (default ~/.johnstudio/server_token) and export it to Vite as
#    VITE_JOHNSTUDIO_TOKEN so the client authenticates against the API.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# ---------------------------------------------------------------------------
# 1. Select a node that can load the rollup native binary.
# ---------------------------------------------------------------------------
if [ -s "$HOME/.nvm/nvm.sh" ]; then
  # shellcheck disable=SC1091
  export NVM_DIR="$HOME/.nvm"
  . "$NVM_DIR/nvm.sh"
  # Prefer the version already installed; fall back to LTS.
  if [ -d "$NVM_DIR/versions/node" ] && ls -1 "$NVM_DIR/versions/node" >/dev/null 2>&1; then
    nvm use --silent "$(ls -1 "$NVM_DIR/versions/node" | sort -V | tail -1)" >/dev/null
  else
    nvm install --lts --silent >/dev/null
    nvm use --lts --silent >/dev/null
  fi
fi

# Diagnostic: if the resolved node still has library-validation enabled and
# isn't entitled to bypass it, vite will dlopen-fail on the rollup binary.
# Tell the user up front rather than dumping a 30-line stack trace.
NODE_BIN="$(command -v node || true)"
if [ -z "$NODE_BIN" ]; then
  echo "error: no node binary on PATH. Install Node.js (or nvm) and retry." >&2
  exit 1
fi
case "$(uname -s)" in
  Darwin)
    if ! codesign -d --entitlements - "$NODE_BIN" 2>/dev/null \
         | grep -q "disable-library-validation"; then
      echo "warning: $NODE_BIN does not declare disable-library-validation;" >&2
      echo "         vite may fail to load rollup's native binary." >&2
      echo "         Install Node via nvm: 'nvm install --lts'." >&2
    fi
    ;;
esac

# ---------------------------------------------------------------------------
# 2. Pull in the loopback bearer token.
# ---------------------------------------------------------------------------
TOKEN_DIR="${JOHNSTUDIO_HOME:-$HOME/.johnstudio}"
TOKEN_FILE="$TOKEN_DIR/server_token"

if [ -r "$TOKEN_FILE" ]; then
  VITE_JOHNSTUDIO_TOKEN="$(cat "$TOKEN_FILE")"
  export VITE_JOHNSTUDIO_TOKEN
else
  echo "warning: $TOKEN_FILE not found — start the backend first (johnstudio server)." >&2
  echo "         UI will load but every authed request will return 401 until the backend writes the token." >&2
fi

cd "$HERE/../desktop"
if [ ! -d node_modules ]; then
  echo "Installing UI dependencies..."
  npm install
fi
exec npm run dev
