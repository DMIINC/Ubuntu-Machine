# Reolink Viewer

GTK 3 + GStreamer live viewer for Reolink cameras on Ubuntu (Python, apt packages only).

## Layout
- `reolink_viewer/config.py`: Camera model, Reolink URL building (RTSP/RTMP/FLV), config at `~/.config/reolink-viewer/config.json` (0600, holds passwords)
- `reolink_viewer/player.py`: `CameraTile` (playbin, reconnect/watchdog), video outputs (`wayland`/`gl`/`cuda`/`cpu`), `FramePacer`, GPU-only mode
- `reolink_viewer/display.py`: forces `GDK_BACKEND=x11` on NVIDIA + Wayland, before Gtk is imported
- `reolink_viewer/app.py`: main window, grid, menus, shortcuts
- `diagnose.sh`: collects system/decoder/network/fps info, masks passwords
- Tests: `python3 -m pytest tests` (no GTK needed)

## Run / debug
- `./reolink-viewer -v` logs decoder, output kind, and every 5 s: fps shown, drops (overflow / late / display), smoothing buffer, app CPU
- Env: `REOLINK_VIEWER_SINK=wayland|gl|cuda|cpu`, `REOLINK_VIEWER_SYNC=0` (disable pacer), `REOLINK_VIEWER_NATIVE_WAYLAND=1`
- Use `/usr/bin/python3` (system python with PyGObject)

## Target machine findings (Media-PC)
- i9-9900X, RTX 5060 Ti, Ubuntu (GNOME Wayland), GStreamer 1.28.2
- Camera "Garage South" 192.168.1.221: Clear 3840x2160 @ 25 fps H.265 6144 kbps; Fluent 640x360 @ 15 fps
- `nvh265dec` decodes. `gtkglsink` fails on native Wayland (NVIDIA) but works under XWayland: 25 fps, 0 dropped
- `gtkwaylandsink` only reached ~20 fps; the CPU drawing path ~9 fps (4K) to ~20 fps (scaled)
- CUDA convert elements (`cudaconvertscale`) are not available (probably libnvrtc is missing)
- The camera sends frames in bursts (instantaneous 8-53 fps) with unreliable timestamps. Sink `sync=true` collapsed to 2-3 fps, so frames are paced by `FramePacer` using arrival times. Its cushion settled around 0.6-0.75 s
- The user wants all video work on the GPU (GPU-only mode is the default)

## Open items
- Confirm the decoder outputs GL/CUDA memory rather than system RAM (look for the `decoded video: ... in ...` log line). App CPU is 20-34% of one core
- A smaller pacer cushion would need a less bursty source; HTTP-FLV (`protocol: flv`) is untested on this camera
- The decode-only test in `diagnose.sh` often reads ~15.7 fps while the app shows ~25 fps. Not yet explained
