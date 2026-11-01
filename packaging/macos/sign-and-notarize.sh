#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Signs dist/Lightning Monitor Bridge.app with a Developer ID, notarises it, staples the ticket and writes
# dist/release/lightning-monitor-bridge-macos-<arch>.zip.
#
#   CODESIGN_IDENTITY="Developer ID Application: Name (TEAMID)" \
#   NOTARY_KEY_ID=... NOTARY_ISSUER_ID=... NOTARY_KEY_PATH=/path/AuthKey_XXXX.p8 \
#   packaging/macos/sign-and-notarize.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
app="$root/dist/Lightning Monitor Bridge.app"
entitlements="$root/packaging/macos/entitlements.plist"
arch="$(uname -m)"
release="$root/dist/release"
archive="$release/lightning-monitor-bridge-macos-$arch.zip"
: "${CODESIGN_IDENTITY:?set CODESIGN_IDENTITY to your Developer ID Application identity}"
: "${NOTARY_KEY_ID:?}" "${NOTARY_ISSUER_ID:?}" "${NOTARY_KEY_PATH:?}"
[[ -d "$app" ]] || { echo "Build first: python packaging/build.py" >&2; exit 1; }

echo "==> Signing libraries inside the app"
while IFS= read -r -d '' file; do
    if file -b "$file" | grep -q "Mach-O"; then
        codesign --force --timestamp --options runtime --sign "$CODESIGN_IDENTITY" "$file"
    fi
done < <(find "$app/Contents" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) ! -path "*/MacOS/*" -print0)

echo "==> Signing the app"
codesign --force --timestamp --options runtime --entitlements "$entitlements" --sign "$CODESIGN_IDENTITY" "$app"
codesign --verify --deep --strict --verbose=2 "$app"

echo "==> Notarising"
mkdir -p "$release"
submission="$root/dist/notarise.zip"
rm -f "$submission"
ditto -c -k --keepParent "$app" "$submission"
xcrun notarytool submit "$submission" --key "$NOTARY_KEY_PATH" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER_ID" --wait
xcrun stapler staple "$app"
spctl --assess --type execute --verbose=2 "$app"

rm -f "$archive" "$submission"
ditto -c -k --keepParent "$app" "$archive"
echo "Wrote $archive"
