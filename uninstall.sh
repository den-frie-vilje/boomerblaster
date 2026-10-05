#!/bin/sh
# floorfill uninstaller: stops the server, removes the LaunchAgent and the
# command. Configuration, the listener page and the log are left in place;
# delete them yourself for a clean slate:
#   ~/.config/floorfill
#   ~/Library/Application Support/floorfill
#   ~/Library/Logs/floorfill.log
set -eu

LABEL="dk.denfrievilje.floorfill"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if command -v floorfill >/dev/null 2>&1; then
    floorfill stop
else
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "LaunchAgent removed"
fi

if [ -f "$HOME/.local/bin/floorfill" ]; then
    rm -f "$HOME/.local/bin/floorfill"
    echo "removed $HOME/.local/bin/floorfill"
fi

if command -v floorfill >/dev/null 2>&1; then
    echo "a floorfill remains on PATH (probably Homebrew): brew uninstall floorfill"
fi

echo "done; config, listener page and log left in place"
