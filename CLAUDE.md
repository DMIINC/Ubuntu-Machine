# Reolink Viewer

GTK 3 + GStreamer live viewer for Reolink cameras on Ubuntu (Python, apt packages only).

## Layout
- `reolink_viewer/config.py`: Camera model, Reolink URL building (RTSP/RTMP/FLV), config at `~/.config/reolink-viewer/config.json` (0600, holds passwords)
- `reolink_viewer/player.py`: `CameraTile` (playbin, reconnect/watchdog), video outputs (`wayland`/`gl`/`cuda`/`cpu`), `FramePacer`, GPU-only mode
- `reolink_viewer/display.py`: sets `GST_GL_API=opengl3` on Wayland before GTK/GStreamer start (native Wayland is the default)
- `reolink_viewer/app.py`: main window, grid, menus, shortcuts
- `diagnose.sh`: collects system/decoder/network/fps info, masks passwords
- Tests: `python3 -m pytest tests` (no GTK needed)

## Run / debug
- `./reolink-viewer -v` logs decoder, output kind, and every 5 s: fps shown, drops (overflow / late / display), smoothing buffer, app CPU
- Env: `REOLINK_VIEWER_SINK=wayland|gl|cuda|cpu`, `REOLINK_VIEWER_SYNC=0` (disable pacer), `GDK_BACKEND=x11` (XWayland, not recommended)
- Use `/usr/bin/python3` (system python with PyGObject)

## Target machine findings (Media-PC)
- i9-9900X, RTX 5060 Ti, Ubuntu (GNOME Wayland), GStreamer 1.28.2
- Camera "Garage South" 192.168.1.221: Clear 3840x2160 @ 25 fps H.265 6144 kbps; Fluent 640x360 @ 15 fps
- `nvh265dec` decodes into `memory:GLMemory` for `gtkglsink`: 25 fps, 0 dropped, natively on Wayland. Without `GST_GL_API=opengl3`, GStreamer's compat-profile context can't share GTK's core-profile one (`EGL_BAD_CONTEXT`)
- Monitor is 2560x1440 at 167% scale with `xwayland-native-scaling`, so XWayland runs at 200% (3072x1728) and GNOME shrinks X11 windows. Through XWayland the viewer was sometimes drawn shifted ~100 px down or 1.2x too large over other windows; that's why it no longer uses XWayland
- `gtkglsink` and `gtkwaylandsink` stay bound to the widget's first window surface. Re-parenting the video widget (grid.remove + attach) left gl drawing nowhere and wayland stalled waiting for frame callbacks, which blanked the video after Maximize. `relayout()` now hides/shows tiles instead, and a tile whose widget is re-realized rebuilds its sink and playbin (a new playbin, because the old one caches the previous GL display/context)
- `gtkwaylandsink` reaches ~25 fps with the 50-frame queue but gets frames in system RAM; the CPU drawing path ~9 fps (4K) to ~20 fps (scaled)
- CUDA convert elements (`cudaconvertscale`) are not available (probably libnvrtc is missing)
- The camera sends frames in bursts (instantaneous 8-53 fps) with unreliable timestamps. Sink `sync=true` collapsed to 2-3 fps, so frames are paced by `FramePacer` using arrival times. Its cushion settled around 0.6-0.75 s
- The user wants all video work on the GPU (GPU-only mode is the default)

## Open items
- Confirmed: default launch (native Wayland, `gl` output) decodes into `memory:GLMemory`, 0 overflow drops after the 50-frame queue. The `wayland` output decodes into system RAM. App CPU is ~40-55% of one core on the `gl` output under Wayland (higher than the 20-38% seen through XWayland); worth checking whether GTK 3 reads the GL area back to the CPU there
- Testing without a screen: a GTK Python harness can drive `MainWindow` (maximize/restore/menu) and read the sink counters, but those only prove frames reached the sink, not the screen. X11 `GetImage` can't read native Wayland windows
- Residual: 0-6 "late" pacer drops per 5 s (bursts beyond cushion + 0.5 s)
- A smaller pacer cushion would need a less bursty source; HTTP-FLV (`protocol: flv`) is untested on this camera
- Solved: the decode-only test in `diagnose.sh` read ~15.7 fps because `rtspsrc ! decodebin` linked the camera's AAC audio pad (16 kHz / 1024 samples = 15.6 packets/s). The test now filters to video and prints the decoder and size: main 3840x2160 `nvh265dec` ~25 fps, sub 640x360 `nvh264dec` ~15 fps, 0 dropped
