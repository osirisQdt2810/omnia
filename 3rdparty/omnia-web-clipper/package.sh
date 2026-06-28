#!/usr/bin/env bash
# Package the extension into an upload-ready .zip for the Chrome Web Store.
# The zip has manifest.json at its ROOT (a Web Store requirement). Run from anywhere:
#   bash 3rdparty/omnia-web-clipper/package.sh
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
version="$(grep -oE '"version"[^,]*' "$dir/manifest.json" | grep -oE '[0-9][0-9.]*')"
out="$dir/../omnia-web-clipper-${version}.zip"

rm -f "$out"
cd "$dir"
# Ship only the runtime files; never the packaging script, OS cruft, or VCS noise.
zip -r "$out" . \
  -x "package.sh" \
  -x ".DS_Store" \
  -x "*/.DS_Store" \
  -x "*.zip" >/dev/null

echo "Built $out"
echo "Upload this zip at https://chrome.google.com/webstore/devconsole (see README §10)."
