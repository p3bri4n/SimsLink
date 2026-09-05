#!/usr/bin/env bash
# One-time setup: adds a SimsLink icon to the applications menu, so it can
# be launched with a double-click instead of a terminal command.
# Run once from the repo: ./packaging/install-launcher.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPS_DIR="$HOME/.local/share/applications"
DEST="$APPS_DIR/simslink.desktop"

mkdir -p "$APPS_DIR"
chmod +x "$REPO_DIR/packaging/simslink-launcher.sh"

sed "s#__REPO_DIR__#$REPO_DIR#g" "$REPO_DIR/packaging/simslink.desktop.template" > "$DEST"
chmod +x "$DEST"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi

echo "SimsLink launcher installed: $DEST"
echo "Look for \"SimsLink\" in your applications menu (some desktops need a log out/in, or a menu refresh, to pick up new entries)."
