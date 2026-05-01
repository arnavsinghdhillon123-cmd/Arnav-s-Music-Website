#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
ROOT="${SCRIPT_DIR:h}"
APP_NAME="Online DAW.app"
DIST_DIR="$ROOT/dist"
APP_PATH="$DIST_DIR/mac-arm64/$APP_NAME"
DMG_NAME="Online DAW macOS.dmg"
DMG_PATH="$DIST_DIR/$DMG_NAME"
STAGING_DIR="$DIST_DIR/.dmg-staging"

if [[ ! -d "$APP_PATH" ]]; then
  echo "Missing app bundle at:"
  echo "  $APP_PATH"
  echo "Build the mac app first with:"
  echo "  npm run dist:mac"
  exit 1
fi

rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

cp -R "$APP_PATH" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"

rm -f "$DMG_PATH"
hdiutil create   -volname "Online DAW"   -srcfolder "$STAGING_DIR"   -ov   -format UDZO   "$DMG_PATH"

rm -rf "$STAGING_DIR"

echo "Created DMG:"
echo "  $DMG_PATH"
