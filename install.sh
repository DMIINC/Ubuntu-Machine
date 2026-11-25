#!/usr/bin/env bash
# Install Reolink Viewer for the current user.
#   ./install.sh            everything: system packages, the app, neolink
#   ./install.sh --update   just copy the app again (after "git pull"); no apt, no sudo
#   ./install.sh --system   install under /usr/local for all users
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX="$HOME/.local"
SUDO=""
UPDATE_ONLY=""
for arg in "$@"; do
    case "$arg" in
        --system) PREFIX=/usr/local; SUDO=sudo ;;
        --update) UPDATE_ONLY=1 ;;
        *) echo "usage: $0 [--update] [--system]" >&2; exit 2 ;;
    esac
done
APP="$PREFIX/share/reolink-viewer"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [[ -z "$UPDATE_ONLY" ]]; then
    echo "==> Installing system packages (needs sudo)"
    sudo apt-get update
    sudo apt-get install -y \
        python3 python3-gi gir1.2-gtk-3.0 \
        gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
        gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav gstreamer1.0-gtk3 \
        libgstrtspserver-1.0-0 curl gcc libc6-dev
    # Not in every Ubuntu release, and only needed for Intel/AMD hardware decoding.
    sudo apt-get install -y gstreamer1.0-vaapi || echo "   (gstreamer1.0-vaapi not available: skipped)"
fi

echo "==> Copying app to $APP"
$SUDO mkdir -p "$APP" "$PREFIX/bin" "$PREFIX/share/applications"
$SUDO rm -rf "$APP/reolink_viewer"
$SUDO cp -r "$SRC/reolink_viewer" "$SRC/reolink-viewer" "$APP/"
$SUDO find "$APP/reolink_viewer" -name __pycache__ -prune -exec rm -rf {} +
$SUDO ln -sf "$APP/reolink-viewer" "$PREFIX/bin/reolink-viewer"
$SUDO install -m 644 "$SRC/data/reolink-viewer.desktop" "$PREFIX/share/applications/"

# neolink serves Reolink battery cameras over RTSP (they have no RTSP server).
# Not in apt: the release binary, pinned and checked.
NEOLINK_VERSION=0.6.3-rc.2
NEOLINK_URL=https://github.com/QuantumEntangledAndy/neolink/releases/download/v0.6.3.rc.2/neolink_linux_x86_64_ubuntu.zip
NEOLINK_SHA256=e54b1e187324cc811fc461c5a8c167b6679bea91cc237a613334b99d3c591898
NEOLINK="$APP/neolink"
if "$NEOLINK" --version 2>/dev/null | grep -q "$NEOLINK_VERSION"; then
    echo "==> neolink $NEOLINK_VERSION already installed"
elif [[ -n "$UPDATE_ONLY" ]]; then
    echo "==> neolink is not installed (run ./install.sh without --update for battery cameras)"
elif [[ "$(uname -m)" == x86_64 ]]; then
    echo "==> Installing neolink $NEOLINK_VERSION (for battery cameras)"
    curl -fsSL -o "$TMP/neolink.zip" "$NEOLINK_URL"
    echo "$NEOLINK_SHA256  $TMP/neolink.zip" | sha256sum -c --quiet
    python3 -m zipfile -e "$TMP/neolink.zip" "$TMP"
    $SUDO install -m 755 "$TMP/neolink_linux_x86_64_ubuntu/neolink" "$NEOLINK"
else
    echo "==> Skipping neolink: no $(uname -m) build pinned here, so battery cameras won't play"
fi

# A small library loaded into neolink that removes the 20 s stall on a battery
# camera's first connection (what it does and why: data/neolink_shim.c).
# Without it everything still works, only slower.
if [[ -x "$NEOLINK" ]] && command -v gcc >/dev/null; then
    echo "==> Building the battery-camera wake speed-up"
    if gcc -shared -fPIC -O2 -o "$TMP/neolink_shim.so" "$SRC/data/neolink_shim.c" -ldl; then
        $SUDO install -m 755 "$TMP/neolink_shim.so" "$APP/neolink_shim.so"
    else
        echo "   (couldn't build it: battery cameras will wake the slower way)"
    fi
elif [[ -x "$NEOLINK" ]]; then
    echo "==> gcc is not installed: battery cameras will wake the slower way (sudo apt install gcc libc6-dev, then run this again)"
fi

command -v update-desktop-database >/dev/null && \
    $SUDO update-desktop-database "$PREFIX/share/applications" || true

echo
echo "Done. Start it from the app menu (\"Reolink Viewer\") or run: reolink-viewer"
[[ ":$PATH:" == *":$PREFIX/bin:"* ]] || echo "Note: add $PREFIX/bin to your PATH (log out/in usually does it)."
