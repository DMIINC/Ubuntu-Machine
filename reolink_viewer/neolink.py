"""Runs neolink, which serves Reolink battery cameras over RTSP on this computer.

Battery cameras sleep with every port closed and wake only for Reolink's own
protocol (Baichuan, over UDP). neolink (github.com/QuantumEntangledAndy/neolink)
finds them on the LAN by UID and logs in, which wakes them. Reolink's servers
are never contacted: discovery is a local broadcast only.

neolink runs only while a battery camera is being watched, streaming only what
is shown; stopping it disconnects, and the camera goes back to sleep. Its own
pause/resume (`pause.on_client` + `idle_disconnect`) was tried first, but after
an idle period it often sent nothing for 20-40 s. Measured on the Reolink Duo
(2026-10-04): from asleep to video on both lenses ~20 s.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from gi.repository import GLib

from .config import NEOLINK_PORT, Camera

log = logging.getLogger(__name__)

# neolink exits on a rejected login instead of retrying, and so must we:
# every rejection counts toward the camera's lockout.
LOGIN_REJECTED = "Login credentials were not accepted"
# After neolink exits on its own, wait this long before starting it again.
MIN_RESTART_INTERVAL_S = 10
STREAM_PATHS = {"main": "mainStream", "sub": "subStream"}
_LINE = re.compile(r"^\[\S+ +(?P<level>[A-Z]+) +(?P<module>[\w:]+)\] (?P<text>.*)$")
_AVAILABLE = re.compile(r"Available at /(\w+)/")


def find_binary() -> str | None:
    """neolink from REOLINK_VIEWER_NEOLINK, the install folder, or PATH."""
    if os.environ.get("REOLINK_VIEWER_NEOLINK"):
        candidates = [Path(os.environ["REOLINK_VIEWER_NEOLINK"])]
    else:
        candidates = [
            Path(__file__).resolve().parent.parent / "neolink",  # install.sh puts it here
            Path.home() / ".local/share/reolink-viewer/neolink",
            Path("/usr/local/share/reolink-viewer/neolink"),
        ]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which("neolink")


def _toml(value) -> str:
    return json.dumps(value)  # JSON strings and booleans are valid TOML


def config_text(awake: list[tuple[Camera, str]]) -> str:
    """neolink's config for these (camera, "main" | "sub") pairs. Cameras are
    mounted by id, which never changes; channel 2 is a dual-lens camera's second lens."""
    lines = ['bind = "127.0.0.1"', f"bind_port = {NEOLINK_PORT}"]
    for cam, stream in awake:
        lines += [
            "",
            "[[cameras]]",
            f"name = {_toml(cam.id)}",
            f"username = {_toml(cam.username)}",
            f"password = {_toml(cam.password)}",
            f"uid = {_toml(cam.uid.strip())}",
            f"channel_id = {max(cam.channel, 1) - 1}",
            'discovery = "local"',  # LAN broadcast only, never Reolink's servers
            f"stream = {_toml(STREAM_PATHS.get(stream, 'subStream'))}",
            # Until the stream format is known, refuse clients rather than serve
            # a test pattern the viewer would take for video.
            "use_splash = false",
            "push_notifications = false",  # these go through Google and Reolink
            "update_time = false",
        ]
    return "\n".join(lines) + "\n"


def _die_with_parent() -> None:
    # Runs in the child before exec: don't outlive the viewer if it crashes.
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


class Neolink:
    """Runs one neolink process for the battery cameras being watched."""

    def __init__(self, on_login_rejected: Callable[[str], None]):
        self._on_login_rejected = on_login_rejected
        self.binary = find_binary()
        self._cameras: dict[str, Camera] = {}  # battery cameras by id
        self._awake: dict[str, str] = {}  # id -> stream being watched
        self._serving: dict[str, str] = {}  # what the running process serves
        self._ready: set[str] = set()  # ids whose stream neolink has mounted
        self._rejected: set[str] = set()  # ids whose login the camera refused
        self._proc: subprocess.Popen | None = None
        self._crashed = 0.0  # when neolink last exited on its own
        self._sync_id = 0
        self._path = Path(GLib.get_user_runtime_dir()) / "reolink-viewer" / "neolink.toml"

    def apply(self, cameras: list[Camera]) -> None:
        """Take in added, edited or removed cameras."""
        old = self._cameras
        self._cameras = {c.id: c for c in cameras if c.on_demand and c.uid.strip()}
        for cam in self._cameras.values():
            prev = old.get(cam.id)
            if prev is None or (prev.username, prev.password, prev.uid, prev.channel) != (
                cam.username, cam.password, cam.uid, cam.channel,
            ):
                self._rejected.discard(cam.id)  # edited login: worth one more try
                if cam.id in self._serving:
                    self._serving = {}  # restart with the new settings
        self._schedule_sync()

    def acquire(self, cam: Camera, stream: str) -> str | None:
        """Wake a camera and serve this stream. Returns why it can't, or None."""
        if not self.binary:
            return "neolink is not installed (run install.sh)"
        if cam.id in self._rejected:
            return "Login rejected: check the password (Edit…)"
        self._cameras[cam.id] = cam
        self._awake[cam.id] = stream
        self._schedule_sync()
        return None

    def release(self, cam: Camera) -> None:
        """Let a camera sleep (once no battery camera is watched)."""
        if self._awake.pop(cam.id, None) is not None:
            self._schedule_sync()

    def ready(self, cam: Camera) -> bool:
        """Whether to connect to this camera's stream yet: once neolink serves the
        streams of all the cameras it's waking. A client that connected earlier,
        even right after its own stream was up while the other lens was still
        starting, often got no video for 20 s (then 503)."""
        return (
            self._proc is not None and cam.id in self._serving
            and self._ready >= self._serving.keys()
        )

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        self._serving = {}
        self._ready.clear()
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._path.unlink(missing_ok=True)  # it holds passwords

    def _schedule_sync(self) -> None:
        # Coalesce: waking both lenses, or switching a stream (stop + start),
        # should restart neolink once.
        if not self._sync_id:
            self._sync_id = GLib.idle_add(self._sync)

    def _sync(self) -> bool:
        self._sync_id = 0
        wanted = {
            i: stream for i, stream in self._awake.items()
            if i in self._cameras and i not in self._rejected
        }
        running = self._proc is not None and self._proc.poll() is None
        if running and wanted and wanted.items() <= self._serving.items():
            # A camera going to sleep doesn't restart neolink for the others (that
            # would cut their video); it disconnects once none is watched.
            return False
        if wanted and not running and time.monotonic() - self._crashed < MIN_RESTART_INTERVAL_S:
            # Don't hammer a camera with logins if neolink keeps exiting; tiles retry.
            return False
        self.stop()
        if not wanted or not self.binary:
            return False
        awake = [(self._cameras[i], stream) for i, stream in wanted.items()]
        text = config_text(awake)
        cameras = [cam for cam, _ in awake]
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        log.info("starting neolink for %s", ", ".join(c.name for c in cameras))
        self._proc = subprocess.Popen(
            [self.binary, "rtsp", "--config", str(self._path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            preexec_fn=_die_with_parent,
        )
        self._serving = wanted
        threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()
        return False

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            GLib.idle_add(self._on_line, line.rstrip())
        proc.wait()
        GLib.idle_add(self._on_exit, proc)

    def _on_line(self, line: str) -> bool:
        if not line.strip():
            return False
        names = {c.id: c.name for c in self._cameras.values()}
        m = _LINE.match(line)
        level, text = (m["level"], m["text"]) if m else ("DEBUG", line)
        cam_id, _, rest = text.partition(": ")
        if cam_id in names:
            text = f"{names[cam_id]}: {rest}"
        mounted = _AVAILABLE.search(text)
        if mounted and mounted[1] in self._serving:
            self._ready.add(mounted[1])
        if LOGIN_REJECTED in text and cam_id in names:
            self._rejected.add(cam_id)
            self._on_login_rejected(cam_id)
        if level == "ERROR":
            log.warning("neolink: %s", text)
        elif level in ("WARN", "INFO") and m:
            log.info("neolink: %s", text)
        else:
            log.debug("neolink: %s", text)  # e.g. GStreamer criticals while a stream starts
        return False

    def _on_exit(self, proc: subprocess.Popen) -> bool:
        if proc is self._proc:  # not stopped by us
            log.warning("neolink exited (code %s)", proc.returncode)
            self._proc = None
            self._serving = {}
            self._ready.clear()
            self._crashed = time.monotonic()
        return False
