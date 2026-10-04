# Reolink Viewer

GTK 3 + GStreamer live viewer for Reolink cameras on Ubuntu (Python, apt packages only).

## Layout
- `reolink_viewer/config.py`: Camera model, Reolink URL building (RTSP/RTMP/FLV), config at `~/.config/reolink-viewer/config.json` (0600, holds passwords)
- `reolink_viewer/player.py`: `CameraTile` (playbin, reconnect/watchdog), video outputs (`wayland`/`gl`/`cuda`/`cpu`), `FramePacer`, GPU-only mode
- `reolink_viewer/display.py`: sets `GST_GL_API=opengl3` on Wayland before GTK/GStreamer start (native Wayland is the default)
- `reolink_viewer/neolink.py`: runs neolink (battery cameras → RTSP on 127.0.0.1:18554) only while battery tiles are awake, config in `$XDG_RUNTIME_DIR/reolink-viewer/neolink.toml` (0600, deleted when it stops)
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
- GPU memory is tight: Ollama (model pinned "Forever", ~11 GB) + Whisper (~2 GB) leave ~2.3-2.7 GB of the 16 GB. On 2026-10-02, three cameras all on the 4K main stream ran it out (`NVRM ... NV_ERR_NO_MEMORY` in `journalctl -k`): GNOME couldn't allocate window buffers, and Claude and ChatGPT crashed
- Measured viewer GPU memory (`gl` output, `nvidia-smi` per process): 4K main ~1.2-1.6 GB per camera; sub ~200 MB per camera with a CUDA context each, ~65-100 MB once one CUDA context is shared (`_share_cuda_context`). 5 tiles on sub: ~390 MB; maximized 4K peak ~1.6-1.8 GB. The `wayland` output's 4K decode is only ~330 MB (frames in system RAM)
- With several cameras the grid plays the balanced (`ext`) stream (`grid_stream` setting), main only when maximized. Reolink serves ext only over RTMP/FLV, so `build_url` sends RTSP cameras' ext to RTMP port 1935. Garage South (RLC-811A) ext = 896x512 @ 20 fps H.264, 0 drops, ~70 MB GPU per tile more than sub. Front Porch / Garage East / Driveway sub = 640x360 @ ~10 fps, Garage South sub 15 fps
- Reolink RTMP and the HTTP API reject passwords that need URL-encoding (RTSP accepts them). On 2026-10-02 Front Porch, Garage East and Driveway had such a password, and each RTMP/API try cost a login attempt (Front Porch fell to 5 left before lockout). `rtmp_safe()` keeps those cameras on sub with no RTMP attempt. Don't probe cameras with failing logins. HTTP-FLV failed on all cameras (Garage South: `httpEnable` 0, HTTPS only)
- Reolink's RTMP doesn't URL-decode the query: `%21` is rejected but a literal `!` works (verified 2026-10-04, ext 896x512). `rtmp2src` (playbin's RTMP source, rank primary+1) passes everything after the last `/` verbatim. Reolink requires a symbol in new passwords, so the user can't use letters and digits only. `QUERY_LITERAL` in config.py lists what's sent unencoded
- RTMP is turned off on Garage East (RLC-811A) and Water Well (RLC-842A, 192.168.1.178): port 1935 is open but drops the connection during the handshake, before any login, so trying costs no login attempt. They play sub in the grid. Read with `neolink services --config X <cam> rtmp get` (logs in over port 9000; `on` would change the camera's setting, ask the user first)
- neolink over port 9000 (Baichuan) accepts any password characters, but the RLC-811A's stream list over Baichuan has only main and sub, so neolink can't provide the balanced stream
- Battery camera "Driveway1": Reolink Duo, hardware BIPC_523MIX32…, firmware v3.0.0.1045_2206131045, 192.168.1.35 (UID is in the Reolink app's Device Info; don't commit it). Asleep, every TCP port is closed (incl. 9000) and ping takes 140-530 ms; the TI Wi-Fi chip answers on its own (MAC 60:e8:5b, TTL 128). The user wants no Reolink servers: neolink uses `discovery = "local"` and `push_notifications = false`
- neolink 0.6.3-rc.2 facts (from its source): a rejected login is not retried and the whole process exits (the viewer then leaves that camera out until it's edited). With `use_splash = false` it answers 400 until it knows a stream's format ("Available at" in its log). Default `discovery` is `relay` (Reolink's servers): always set it. It exits on SIGTERM
- Its `pause.on_client` + `idle_disconnect` resume was unreliable on the Duo: after an idle period, "Enabling Client" was followed by nothing for 20-44 s. So the viewer runs neolink only while battery tiles are awake (no pause, one stream per camera) and stops it when all sleep
- The Duo is dual-lens: channel 0 and 1 (`channel_id`), each 2560x1440 H.265 main (`nvh265dec`). Two config entries with the same UID (Driveway, Driveway 2); maximizing either shows both side by side (`MainWindow._lenses`)
- Duo wake timing (2026-10-04): local discovery 0-9 s (9 s from deep sleep), neolink "Available" at 14-17 s. A standalone client then got video in ~4 s, but in the viewer the first connection after "Available" usually stalled (no answer, then 503) and the retry worked, so live at 40-45 s. Connecting before "Available" made it worse (20+ s stalls), so tiles wait for `Neolink.ready()` (all woken cameras mounted) and use a 10 s rtspsrc `tcp-timeout`. Cause not found; possibly the camera's Wi-Fi (ping 140-530 ms asleep)
- The app-menu launcher runs a copy in `~/.local/share/reolink-viewer` (made by `install.sh`). Re-copy `reolink_viewer/` there after changes, or the user keeps running old code
- After a 4K maximize the process keeps ~0.9-1.3 GB that the NVIDIA driver holds for reuse: it shrinks over time and doesn't grow with further maximizes, so rebuilding pipelines doesn't help
- Never let a `gtkglsink` be freed: on Wayland, finalizing it terminates the EGL display GTK shares, and all GL drawing then fails (`eglMakeCurrent failed`, then fallback/segfault). Replaced outputs are kept alive by a reference cycle (see `VideoSink`)
- A test harness that imports the app must call `display.prepare_display()` first, or the `gl` output fails and silently falls back to `wayland`, which gives misleading GPU memory numbers

## Open items
- Confirmed: default launch (native Wayland, `gl` output) decodes into `memory:GLMemory`, 0 overflow drops after the 50-frame queue. The `wayland` output decodes into system RAM. App CPU is ~40-55% of one core on the `gl` output under Wayland (higher than the 20-38% seen through XWayland); worth checking whether GTK 3 reads the GL area back to the CPU there
- Testing without a screen: a GTK Python harness can drive `MainWindow` (maximize/restore/menu) and read the sink counters, but those only prove frames reached the sink, not the screen. X11 `GetImage` can't read native Wayland windows
- Residual: 0-6 "late" pacer drops per 5 s (bursts beyond cushion + 0.5 s)
- A smaller pacer cushion would need a less bursty source; HTTP-FLV (`protocol: flv`) is untested on this camera
- Solved: the decode-only test in `diagnose.sh` read ~15.7 fps because `rtspsrc ! decodebin` linked the camera's AAC audio pad (16 kHz / 1024 samples = 15.6 packets/s). The test now filters to video and prints the decoder and size: main 3840x2160 `nvh265dec` ~25 fps, sub 640x360 `nvh264dec` ~15 fps, 0 dropped
