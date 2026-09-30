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
    gstreamer1.0-libav gstreamer1.0-gtk3 gstreamer1.0-vaapi

echo "==> Copying app to $PREFIX/share/reolink-viewer"
$SUDO mkdir -p "$PREFIX/share/reolink-viewer" "$PREFIX/bin" "$PREFIX/share/applications"
$SUDO rm -rf "$PREFIX/share/reolink-viewer/reolink_viewer"
$SUDO cp -r "$SRC/reolink_viewer" "$SRC/reolink-viewer" "$PREFIX/share/reolink-viewer/"
$SUDO ln -sf "$PREFIX/share/reolink-viewer/reolink-viewer" "$PREFIX/bin/reolink-viewer"
$SUDO install -m 644 "$SRC/data/reolink-viewer.desktop" "$PREFIX/share/applications/"
command -v update-desktop-database >/dev/null && \
    $SUDO update-desktop-database "$PREFIX/share/applications" || true

echo
echo "Done. Start it from the app menu (\"Reolink Viewer\") or run: reolink-viewer"
[[ ":$PATH:" == *":$PREFIX/bin:"* ]] || echo "Note: add $PREFIX/bin to your PATH (log out/in usually does it)."
