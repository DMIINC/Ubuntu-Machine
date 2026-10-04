# Reolink Viewer for Ubuntu

A lightweight desktop app for watching live streams from **Reolink cameras, NVRs and Home Hubs** on Ubuntu.
It's built with Python, GTK 3 and GStreamer, which are all native Ubuntu packages. You don't need pip or a Reolink cloud account.

![screenshot](data/screenshot.png)

## Features

- Shows several cameras at once in an automatic grid, or you can pick 1–5 columns
- Supports **RTSP** (default), **RTMP**, **HTTP-FLV** or any custom stream URL
- Supports H.264 and H.265 (HEVC) cameras, and NVR channels 1–64
- With more than one camera, the grid plays each camera's **balanced** stream (e.g. 896×512), whatever stream the camera is set to (menu: **Grid stream**). A 4K main stream holds over 1 GB of GPU memory, so a grid of them can run the graphics card out of memory. Double-clicking a camera maximizes it and switches to the **main** (4K/HD) stream. The other cameras pause to save bandwidth and GPU memory.
- Reolink serves the balanced stream only over RTMP, so RTSP cameras fetch it over RTMP. Reolink's RTMP rejects passwords with symbols other than `- _ . ~ !` (RTSP accepts any), so a camera with another symbol in its password plays its **sub** stream in the grid instead. The viewer never tries RTMP with such a password, because failed logins count toward the camera's lockout. A camera with RTMP turned off also plays sub.
- **Battery cameras** (Argus, battery Duo, …) through [neolink](https://github.com/QuantumEntangledAndy/neolink), which the viewer runs only while one is being watched. They sleep until you double-click their tile, go back to sleep when you return to the grid, and after 5 minutes at most. A dual-lens camera shows both lenses side by side. Reolink's servers aren't used: neolink finds the camera on your network by its UID.
- Reconnects automatically with backoff when a camera drops, errors or stops sending video
- Saves **snapshots** as PNG files to `~/Pictures/Reolink/`
- Optional audio for each camera
- Includes fullscreen mode and keyboard shortcuts
- Passwords are hidden everywhere in the UI and in the logs

## Install

```bash
git clone <this repo> reolink-viewer
cd reolink-viewer
./install.sh            # installs for your user (~/.local); use --system for /usr/local
```

The script installs these apt packages: `python3-gi`, `gir1.2-gtk-3.0`, GStreamer base, good, bad, ugly and libav plugins, `gstreamer1.0-gtk3`, `gstreamer1.0-vaapi` and `libgstrtspserver-1.0-0`. It downloads neolink 0.6.3-rc.2 from its GitHub release (checking the file's SHA-256), and adds **Reolink Viewer** to your applications menu.

To run it without installing (once the apt packages are present):

```bash
./reolink-viewer
```

## Camera setup

1. In the Reolink app or web UI, open **Settings → Network → Advanced → Server Settings** (the wording varies by model and firmware). Turn on **RTSP** and **RTMP** (RTMP carries the balanced stream the grid uses).
2. In Reolink Viewer, click **+** and enter the camera's IP address, username (usually `admin`) and password.
3. Leave the protocol set to RTSP and the port at 554. For cameras encoding H.265 (many 4K/8MP models), set **Codec** to **H.265**.
4. For an **NVR** or Home Hub, use the NVR's IP address and add one entry per camera, with **Channel** set to 1, 2, 3…

### Battery cameras

Battery cameras have no RTSP or RTMP server; the Reolink app wakes them over Reolink's own protocol. Set **Protocol** to **Battery camera (via neolink)**, then enter the camera's **UID** (Reolink app → camera → Settings → Device Info; mind `0` vs `O`), username and password. The grid plays the sub stream and a maximized camera the main stream; there is no balanced stream.

For a **dual-lens** camera (e.g. Reolink Duo), add it twice with the same UID: **Channel** 1 for the first lens and 2 for the second. Double-clicking either one shows both lenses side by side.

The tile shows *Asleep* until you double-click it. Waking the Reolink Duo took 40–45 s to live video on both lenses (neolink is ready after ~15 s; its first connection often stalls and is retried). If the camera can't be reached within 45 s, the tile says so and stops trying. Each wake costs battery, so the camera sleeps again when you go back to the grid, and after 5 minutes. Right-click a tile for **Wake** / **Sleep now**.

URLs the app generates:

| Protocol | URL |
|---|---|
| RTSP | `rtsp://admin:PASS@IP:554/h264Preview_01_main` (or `_sub`, `h265…`) |
| RTMP | `rtmp://IP:1935/bcs/channel0_main.bcs?channel=0&stream=0&user=admin&password=PASS` |
| HTTP-FLV | `http://IP/flv?port=1935&app=bcs&stream=channel0_main.bcs&user=admin&password=PASS` |

To see the exact URL, open the dialog's **URL** preview or hover over a camera tile. The password is masked in both.

### Quick play without saving

```bash
reolink-viewer "rtsp://admin:PASS@192.168.1.50:554/h264Preview_01_main"
```

## Controls

| Action | How |
|---|---|
| Maximize / restore a camera | Double-click it, or press `1`–`9` / `0` / `Esc` |
| Menu for one camera (switch stream, snapshot, reconnect, reorder, edit, remove) | Right-click it |
| Fullscreen | `F11` or `F` |
| Add a camera | `Ctrl+N` |
| Quit | `Ctrl+Q` |

## Configuration

Cameras are stored in `~/.config/reolink-viewer/config.json`. The file is created with `600` permissions because it contains your camera passwords. A good practice is to create a separate **view-only user** on the camera or NVR for this app.

## Troubleshooting

- **`Unauthorized`**: the username or password is wrong. Special characters are handled correctly.
- **Black tile or `No video`**: make sure RTSP is enabled on the camera. Also try the H.265 codec setting, or switch the protocol to RTMP/FLV. For battery cameras, see [Battery cameras](#battery-cameras).
- **Battery camera won't wake**: check the UID, and that the camera has Wi-Fi signal (it must be on the same network as this computer). `reolink-viewer -v` shows neolink's log. If the password is wrong, neolink stops at the first rejection and the tile says so; it tries again only after you edit the camera.
- **Choppy video**: run `reolink-viewer -v` from a terminal. Every 5 seconds each camera logs `NN fps shown, N dropped (gl|sw output)` and names its decoder, which tells you whether the network, decoding or display is the bottleneck. Things to try:
  - Switch **Protocol** to **HTTP-FLV** (port 80). Reolink's RTSP implementation is often the weakest option.
  - In the Reolink app, set the camera's Clear stream to a fixed frame rate (20–25), and consider a 4 Mbps+ bitrate over a wired connection.
  - The app picks the fastest working video output automatically: OpenGL (`gl`) first, where decoding, conversion, scaling and drawing all stay on the GPU, then the Wayland compositor drawing the video directly (`wayland`), NVIDIA CUDA scaling (`cuda`) and multithreaded CPU scaling (`cpu`). Stale frames are dropped rather than queued, so the picture stays live. To force one output, run e.g. `REOLINK_VIEWER_SINK=wayland reolink-viewer`.
  - The app runs natively on Wayland, including on NVIDIA (it sets `GST_GL_API=opengl3` so GStreamer's OpenGL output can share GTK's GL context). Running it through XWayland (`GDK_BACKEND=x11`) works, but with fractional scaling GNOME can draw the window shifted or too large, spilling over other windows.
  - Frames are paced evenly: many cameras (Reolink included) send frames in bursts with unreliable timestamps, so the app measures the real frame rate, keeps a small buffer that adapts to the camera's longest recent pause (0.1–0.8 s), and shows one frame per interval. `REOLINK_VIEWER_SYNC=0` shows frames as soon as they arrive instead: lower latency, uneven motion.
  - **GPU only** (menu, on by default): software video decoders and the CPU drawing paths are never used. If your GPU can't decode a stream, the tile says so instead of falling back to the CPU. Turn it off in the menu if you want CPU fallback.
- **High CPU or GPU memory with many main streams**: keep **Grid stream** on **Balanced** or **Sub**. If other apps crash or the desktop stutters while the viewer runs, check `nvidia-smi`: the GPU memory may be full. Hardware decoding needs working VA-API drivers (`sudo apt install vainfo && vainfo`).
- For detailed logs, run `reolink-viewer -v` from a terminal. For GStreamer-level logs, add `GST_DEBUG=3`.

## Development

```bash
python3 -m pytest tests    # URL building and config tests (no GTK needed)
```

Code layout:

- `reolink_viewer/config.py`: camera model, URL building and saving/loading the config
- `reolink_viewer/player.py`: `CameraTile`, a GStreamer `playbin` + `gtksink` widget with reconnect/watchdog
- `reolink_viewer/dialogs.py`: the add/edit camera dialog
- `reolink_viewer/neolink.py`: runs neolink while battery cameras are watched, and writes its config (in `$XDG_RUNTIME_DIR`, removed when it stops)
- `reolink_viewer/app.py`: main window, grid, menus and shortcuts
