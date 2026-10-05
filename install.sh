#!/bin/sh
# floorfill installer for a clone of this repository: copies the command
# into ~/.local/bin and checks the three programs it drives. Setup itself
# (fetching the listener page, writing the configuration) happens in
# `floorfill init`, which explains what it does. Homebrew users do not
# need this file: brew install den-frie-vilje/tap/floorfill.
set -eu

BIN_DIR="$HOME/.local/bin"
HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew is required for the dependencies: https://brew.sh"; exit 1
fi

missing=""
command -v snapserver >/dev/null 2>&1 || missing="$missing snapcast"
command -v shairport-sync >/dev/null 2>&1 || missing="$missing shairport-sync"
command -v librespot >/dev/null 2>&1 || missing="$missing librespot"

if [ -n "$missing" ]; then
    printf "Missing:%s. Install with brew now? [y/N] " "$missing"
    read -r answer
    if [ "$answer" = "y" ]; then
        # shellcheck disable=SC2086
        brew install $missing
    else
        echo "floorfill needs them before init:"
        echo "  brew install$missing"
    fi
fi

mkdir -p "$BIN_DIR"
install -m 0755 "$HERE/floorfill" "$BIN_DIR/floorfill"
echo "installed $BIN_DIR/floorfill"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not in your PATH; add to your shell profile:"
       echo "  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

echo
echo "Next:"
echo "  floorfill init"
echo "  floorfill start"
echo "  floorfill url"
