#!/usr/bin/env bash
# Install Reolink Viewer for the current user (or system-wide with --system).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
if [[ "${1:-}" == "--system" ]]; then
    PREFIX=/usr/local
    SUDO=sudo
else
    PREFIX="$HOME/.local"
    SUDO=""
fi

echo "==> Installing system packages (needs sudo)"
sudo apt-get update
sudo apt-get install -y \
    python3 python3-gi gir1.2-gtk-3.0 \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav gstreamer1.0-gtk3 gstreamer1.0-vaapi \
    libgstrtspserver-1.0-0 curl

echo "==> Copying app to $PREFIX/share/reolink-viewer"
$SUDO mkdir -p "$PREFIX/share/reolink-viewer" "$PREFIX/bin" "$PREFIX/share/applications"
$SUDO rm -rf "$PREFIX/share/reolink-viewer/reolink_viewer"
$SUDO cp -r "$SRC/reolink_viewer" "$SRC/reolink-viewer" "$PREFIX/share/reolink-viewer/"
$SUDO ln -sf "$PREFIX/share/reolink-viewer/reolink-viewer" "$PREFIX/bin/reolink-viewer"
$SUDO install -m 644 "$SRC/data/reolink-viewer.desktop" "$PREFIX/share/applications/"

# neolink serves Reolink battery cameras over RTSP (they have no RTSP server).
# Not in apt: the release binary, pinned and checked.
NEOLINK_VERSION=0.6.3-rc.2
NEOLINK_URL=https://github.com/QuantumEntangledAndy/neolink/releases/download/v0.6.3.rc.2/neolink_linux_x86_64_ubuntu.zip
NEOLINK_SHA256=e54b1e187324cc811fc461c5a8c167b6679bea91cc237a613334b99d3c591898
NEOLINK="$PREFIX/share/reolink-viewer/neolink"
if "$NEOLINK" --version 2>/dev/null | grep -q "$NEOLINK_VERSION"; then
    echo "==> neolink $NEOLINK_VERSION already installed"
elif [[ "$(uname -m)" == x86_64 ]]; then
    echo "==> Installing neolink $NEOLINK_VERSION (for battery cameras)"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    curl -fsSL -o "$TMP/neolink.zip" "$NEOLINK_URL"
    echo "$NEOLINK_SHA256  $TMP/neolink.zip" | sha256sum -c --quiet
    python3 -m zipfile -e "$TMP/neolink.zip" "$TMP"
    $SUDO install -m 755 "$TMP/neolink_linux_x86_64_ubuntu/neolink" "$NEOLINK"
else
    echo "==> Skipping neolink: no $(uname -m) build pinned here, so battery cameras won't play"
fi
command -v update-desktop-database >/dev/null && \
    $SUDO update-desktop-database "$PREFIX/share/applications" || true

echo
echo "Done. Start it from the app menu (\"Reolink Viewer\") or run: reolink-viewer"
[[ ":$PATH:" == *":$PREFIX/bin:"* ]] || echo "Note: add $PREFIX/bin to your PATH (log out/in usually does it)."
