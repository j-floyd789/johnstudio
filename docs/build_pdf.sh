#!/usr/bin/env bash
# Regenerate docs/USER_GUIDE.pdf from docs/USER_GUIDE.md.
# Requires: pandoc, Google Chrome (macOS path baked in; adjust for Linux).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ ! -x "$CHROME" ]; then
  echo "error: chrome not found at $CHROME" >&2
  echo "edit this script if your chrome lives elsewhere" >&2
  exit 1
fi
if ! command -v pandoc >/dev/null; then
  echo "error: pandoc required. brew install pandoc" >&2
  exit 1
fi

# 1. Markdown → standalone HTML with the CSS inlined
pandoc USER_GUIDE.md \
  --from=gfm --to=html5 --standalone --embed-resources \
  --css=user_guide.css \
  --metadata title="JohnStudio User Guide" \
  -o _user_guide.html

# 2. HTML → PDF via Chrome headless
"$CHROME" \
  --headless=new --disable-gpu \
  --print-to-pdf-no-header \
  --no-pdf-header-footer \
  --print-to-pdf="$HERE/USER_GUIDE.pdf" \
  --virtual-time-budget=8000 \
  "file://$HERE/_user_guide.html"

rm -f _user_guide.html
echo "built: $HERE/USER_GUIDE.pdf ($(wc -c < USER_GUIDE.pdf | awk '{printf "%.0f KB", $1/1024}'), $(python3 -c "from pypdf import PdfReader; print(len(PdfReader('$HERE/USER_GUIDE.pdf').pages))") pages)"
