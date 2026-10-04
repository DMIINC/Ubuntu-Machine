#!/usr/bin/env bash
# Collects everything needed to troubleshoot choppy / delayed video.
# Output goes to the screen and to ~/reolink-diagnose.txt. Passwords are masked.
HERE="$(dirname "$(readlink -f "$0")")"
OUT="$HOME/reolink-diagnose.txt"
mask() { sed -E 's#(://[^:/@]+):[^@]*@#\1:***@#g; s#(password=)[^& ]*#\1***#g'; }

{
echo "=== System"
grep -m1 "model name" /proc/cpuinfo; echo "cores: $(nproc)"
lspci 2>/dev/null | grep -Ei "vga|3d|display"
echo "session: ${XDG_SESSION_TYPE:-?}  GDK_BACKEND: ${GDK_BACKEND:-unset}  gstreamer: $(gst-launch-1.0 --version | sed -n 2p)"

echo; echo "=== Video decoders available (higher rank wins)"
for e in vah264dec vah265dec vaapih264dec vaapih265dec nvh264dec nvh265dec avdec_h264 avdec_h265; do
    rank=$(gst-inspect-1.0 "$e" 2>/dev/null | grep -m1 -i "rank" | sed 's/^ *//')
    [ -n "$rank" ] && echo "$e: $rank"
done
NEOLINK="$(PYTHONPATH="$HERE" /usr/bin/python3 -c 'from reolink_viewer.neolink import find_binary; print(find_binary() or "")')"
echo; echo "neolink (battery cameras): ${NEOLINK:-not installed}${NEOLINK:+ ($("$NEOLINK" --version 2>/dev/null | tail -1))}"
command -v vainfo >/dev/null && { echo; echo "=== vainfo"; vainfo 2>&1 | grep -E "Driver version|VAProfileH26[45]|error" | head -12; }

mapfile -t CAMS < <(PYTHONPATH="$HERE" /usr/bin/python3 -c '
from reolink_viewer.config import load_settings, default_config_path
# Battery cameras are left out: a test would wake them.
for c in [c for c in load_settings(default_config_path()).cameras if not c.on_demand][:2]:
    print(c.name + "\t" + (c.host or "") + "\t" + c.url("main") + "\t" + c.url("sub"))')

for line in "${CAMS[@]}"; do
    IFS=$'\t' read -r name host main sub <<< "$line"
    echo; echo "=== Camera: $name"
    [ -n "$host" ] && ping -c 10 -i 0.2 "$host" 2>&1 | tail -2
    for url in "$main" "$sub"; do
        echo "--- decode-only test (no display), 15s: $url" | mask
        # Filter to video explicitly: with audio on, the first pad to appear
        # may be audio, and the fps counter would then count AAC packets.
        if [[ "$url" == rtsp://* ]]; then
            src=(rtspsrc location="$url" protocols=tcp latency=300
                 ! "application/x-rtp,media=video" ! decodebin)
        else
            src=(uridecodebin uri="$url")
        fi
        log=$(timeout 15 gst-launch-1.0 -v "${src[@]}" ! "video/x-raw(ANY)" \
            ! fpsdisplaysink video-sink=fakesink text-overlay=false sync=false 2>&1)
        dec=$(grep -oE "(nv|va|vaapi|avdec_)h26[45]dec|avdec_h26[45]" <<< "$log" | head -1)
        size=$(grep -oE "video/x-raw.*width=\(int\)[0-9]+, height=\(int\)[0-9]+" <<< "$log" \
            | head -1 | grep -oE "[0-9]+, height=\(int\)[0-9]+" | sed 's/, height=(int)/x/')
        echo "decoder: ${dec:-?}  size: ${size:-?}"
        grep -E "last-message|ERROR" <<< "$log" | tail -2 | sed 's#.*last-message = ##' | mask
    done
done

APP_FILTER="fps shown|decoder|decoded video|output|error|WARNING|GPU-only"
echo; echo "=== App run (40s, window will open)"
# Match the desktop launcher: terminals inside some apps export GDK_BACKEND.
env -u GDK_BACKEND timeout 40 "$HERE/reolink-viewer" -v 2>&1 | grep -E "$APP_FILTER" | tail -30 | mask
} 2>&1 | tee "$OUT"

echo; echo "Saved to $OUT - paste its contents back to Claude."
