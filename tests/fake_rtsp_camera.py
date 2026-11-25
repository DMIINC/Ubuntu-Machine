#!/usr/bin/env python3
"""Fake Reolink camera: an RTSP server with Reolink's URL paths and basic auth.

usage: fake_rtsp_camera.py PORT [PASSWORD]      (user is always "admin", default password "p@ss")
Mounts, for channels 01..03:
  /h264Preview_0N_main  1920x1080 @ 25 fps H.264 (channel 01 also carries AAC audio)
  /h264Preview_0N_sub    640x360  @ 15 fps H.264
Each channel shows a different test pattern with a running clock.
"""
import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtspServer  # noqa: E402

port = sys.argv[1] if len(sys.argv) > 1 else "8554"
password = sys.argv[2] if len(sys.argv) > 2 else "p@ss"
Gst.init(None)
server = GstRtspServer.RTSPServer()
server.set_service(port)
auth = GstRtspServer.RTSPAuth()
token = GstRtspServer.RTSPToken()
token.set_string(GstRtspServer.RTSP_TOKEN_MEDIA_FACTORY_ROLE, "user")
auth.add_basic(GstRtspServer.RTSPAuth.make_basic("admin", password), token)
server.set_auth(auth)
mounts = server.get_mount_points()
for ch, pattern in ((1, "ball"), (2, "smpte"), (3, "snow")):
    for stream, size in (("main", "width=1920,height=1080,framerate=25/1"),
                         ("sub", "width=640,height=360,framerate=15/1")):
        launch = (
            f"( videotestsrc is-live=true pattern={pattern} ! video/x-raw,{size} ! timeoverlay"
            " ! x264enc tune=zerolatency speed-preset=ultrafast key-int-max=25"
            " ! rtph264pay name=pay0 pt=96"
        )
        if ch == 1 and stream == "main":
            launch += (" audiotestsrc is-live=true wave=ticks ! audio/x-raw,rate=16000,channels=1"
                       " ! avenc_aac ! rtpmp4gpay name=pay1 pt=97")
        factory = GstRtspServer.RTSPMediaFactory()
        factory.set_launch(launch + " )")
        factory.set_shared(True)
        perms = GstRtspServer.RTSPPermissions()
        perms.add_permission_for_role("user", "media.factory.access", True)
        perms.add_permission_for_role("user", "media.factory.construct", True)
        factory.set_permissions(perms)
        mounts.add_factory(f"/h264Preview_{ch:02d}_{stream}", factory)
server.attach(None)
print(f"fake camera ready on rtsp://127.0.0.1:{port}  (admin / {password})", flush=True)
GLib.MainLoop().run()
