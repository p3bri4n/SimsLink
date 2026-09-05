#!/usr/bin/env bash
# Launches SimsLink from a desktop icon (double-click, no terminal attached).
# Self-locates the repo root, so it works regardless of where the repo was
# cloned — the .desktop entry just points at this script's own path.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

URL="http://127.0.0.1:8000/"
LOG_FILE="$REPO_DIR/.venv/simslink-launch.log"

notify_error() {
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title="SimsLink" --text="$1" 2>/dev/null
    else
        echo "$1" >&2
    fi
}

# Already running (e.g. the icon got double-clicked twice)? Reopen the
# existing instance instead of trying to start a second server on the
# same port.
if (echo >/dev/tcp/127.0.0.1/8000) 2>/dev/null; then
    xdg-open "$URL" >/dev/null 2>&1 &
    exit 0
fi

if [ -f "$REPO_DIR/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$REPO_DIR/.venv/bin/activate"
fi

if ! command -v simslink >/dev/null 2>&1; then
    notify_error "SimsLink isn't installed yet.

Open a terminal in $REPO_DIR and run:
python -m venv .venv && source .venv/bin/activate && pip install -e \".[dev]\""
    exit 1
fi

if [ ! -f "$REPO_DIR/.env" ]; then
    notify_error "Missing .env file.

Copy .env.example to .env and fill in your Sims 4 folders before launching SimsLink."
    exit 1
fi

if ! simslink 2>"$LOG_FILE"; then
    notify_error "SimsLink failed to start:

$(tail -n 5 "$LOG_FILE")"
    exit 1
fi
