#!/bin/sh
# boomerblaster uninstaller: stops the server, removes the LaunchAgent and the
# command. Configuration, the listener page and the log are left in place;
# delete them yourself for a clean slate:
#   ~/.config/boomerblaster
#   ~/Library/Application Support/boomerblaster
#   ~/Library/Logs/boomerblaster.log
set -eu

LABEL="dk.denfrievilje.boomerblaster"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if command -v boomerblaster >/dev/null 2>&1; then
    boomerblaster stop
else
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "LaunchAgent removed"
fi

if [ -f "$HOME/.local/bin/boomerblaster" ]; then
    rm -f "$HOME/.local/bin/boomerblaster"
    echo "removed $HOME/.local/bin/boomerblaster"
fi

if command -v boomerblaster >/dev/null 2>&1; then
    echo "a boomerblaster remains on PATH (probably Homebrew): brew uninstall boomerblaster"
fi

echo "done; config, listener page and log left in place"
