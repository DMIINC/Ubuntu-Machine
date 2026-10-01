# Reolink Viewer for Ubuntu

A lightweight desktop app for watching live streams from **Reolink cameras, NVRs and Home Hubs** on Ubuntu.
It's built with Python, GTK 3 and GStreamer, which are all native Ubuntu packages. You don't need pip or a Reolink cloud account.

![screenshot](data/screenshot.png)

## Features

- Shows several cameras at once in an automatic grid, or you can pick 1–5 columns
- Supports **RTSP** (default), **RTMP**, **HTTP-FLV** or any custom stream URL
- Supports H.264 and H.265 (HEVC) cameras, and NVR channels 1–64
- The grid uses the low-bandwidth **sub** stream. Double-clicking a camera maximizes it and switches to the **main** (HD) stream. The other cameras pause to save bandwidth.
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

The script installs these apt packages: `python3-gi`, `gir1.2-gtk-3.0`, GStreamer base, good, bad, ugly and libav plugins, `gstreamer1.0-gtk3` and `gstreamer1.0-vaapi`. It also adds **Reolink Viewer** to your applications menu.

To run it without installing (once the apt packages are present):

```bash
./reolink-viewer
```

## Camera setup

1. In the Reolink app or web UI, open **Settings → Network → Advanced → Server Settings** (the wording varies by model and firmware). Turn on **RTSP**, and turn on **RTMP/HTTP** too if you want those protocols.
2. In Reolink Viewer, click **+** and enter the camera's IP address, username (usually `admin`) and password.
3. Leave the protocol set to RTSP and the port at 554. For cameras encoding H.265 (many 4K/8MP models), set **Codec** to **H.265**.
4. For an **NVR** or Home Hub, use the NVR's IP address and add one entry per camera, with **Channel** set to 1, 2, 3…

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
- **Black tile or `No video`**: make sure RTSP is enabled on the camera. Also try the H.265 codec setting, or switch the protocol to RTMP/FLV. Newer battery cameras and some doorbells only stream through a Home Hub or NVR.
- **Choppy video**: run `reolink-viewer -v` from a terminal. Every 5 seconds each camera logs `NN fps shown, N dropped (gl|sw output)` and names its decoder, which tells you whether the network, decoding or display is the bottleneck. Things to try:
  - Switch **Protocol** to **HTTP-FLV** (port 80). Reolink's RTSP implementation is often the weakest option.
  - In the Reolink app, set the camera's Clear stream to a fixed frame rate (20–25), and consider a 4 Mbps+ bitrate over a wired connection.
  - The app picks the fastest working video output automatically: OpenGL (`gl`) first, where decoding, conversion, scaling and drawing all stay on the GPU, then the Wayland compositor drawing the video directly (`wayland`), NVIDIA CUDA scaling (`cuda`) and multithreaded CPU scaling (`cpu`). Stale frames are dropped rather than queued, so the picture stays live. To force one output, run e.g. `REOLINK_VIEWER_SINK=wayland reolink-viewer`.
  - The app runs natively on Wayland, including on NVIDIA (it sets `GST_GL_API=opengl3` so GStreamer's OpenGL output can share GTK's GL context). Running it through XWayland (`GDK_BACKEND=x11`) works, but with fractional scaling GNOME can draw the window shifted or too large, spilling over other windows.
  - Frames are paced evenly: many cameras (Reolink included) send frames in bursts with unreliable timestamps, so the app measures the real frame rate, keeps a small buffer that adapts to the camera's longest recent pause (0.1–0.8 s), and shows one frame per interval. `REOLINK_VIEWER_SYNC=0` shows frames as soon as they arrive instead: lower latency, uneven motion.
  - **GPU only** (menu, on by default): software video decoders and the CPU drawing paths are never used. If your GPU can't decode a stream, the tile says so instead of falling back to the CPU. Turn it off in the menu if you want CPU fallback.
- **High CPU with many main streams**: keep the grid on sub streams (the default). Hardware decoding needs working VA-API drivers (`sudo apt install vainfo && vainfo`).
- For detailed logs, run `reolink-viewer -v` from a terminal. For GStreamer-level logs, add `GST_DEBUG=3`.

## Development

```bash
python3 -m pytest tests    # URL building and config tests (no GTK needed)
```

Code layout:

- `reolink_viewer/config.py`: camera model, URL building and saving/loading the config
- `reolink_viewer/player.py`: `CameraTile`, a GStreamer `playbin` + `gtksink` widget with reconnect/watchdog
- `reolink_viewer/dialogs.py`: the add/edit camera dialog
- `reolink_viewer/app.py`: main window, grid, menus and shortcuts
