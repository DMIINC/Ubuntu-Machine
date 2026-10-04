"""Runs neolink, which serves Reolink battery cameras over RTSP on this computer.

Battery cameras sleep with every port closed and wake only for Reolink's own
protocol (Baichuan, over UDP). neolink (github.com/QuantumEntangledAndy/neolink)
finds them on the LAN by UID, keeps a camera awake while an RTSP client is
connected, and disconnects 30 s after the last one leaves so it can sleep.
Reolink's servers are never contacted: discovery is a local broadcast only.
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
# Each start wakes the battery cameras once to learn their stream format.
MIN_RESTART_INTERVAL_S = 10
_LINE = re.compile(r"^\[\S+ +(?P<level>[A-Z]+) +(?P<module>[\w:]+)\] (?P<text>.*)$")


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


def config_text(cameras: list[Camera]) -> str:
    """neolink's config. Cameras are mounted by id, which never changes."""
    lines = ['bind = "127.0.0.1"', f"bind_port = {NEOLINK_PORT}"]
    for cam in cameras:
        lines += [
            "",
            "[[cameras]]",
            f"name = {_toml(cam.id)}",
            f"username = {_toml(cam.username)}",
            f"password = {_toml(cam.password)}",
            f"uid = {_toml(cam.uid.strip())}",
            'discovery = "local"',  # LAN broadcast only, never Reolink's servers
            'stream = "both"',
            "pause = { on_client = true }",  # stream only while a client watches
            "idle_disconnect = true",
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
    """Keeps one neolink process serving the battery cameras."""

    def __init__(self, on_login_rejected: Callable[[str], None]):
        self._on_login_rejected = on_login_rejected
        self.binary = find_binary()
        self._cameras: list[Camera] = []
        self._rejected: set[str] = set()  # ids whose login the camera refused
        self._proc: subprocess.Popen | None = None
        self._config = ""  # what the running process was started with
        self._started = 0.0
        self._path = Path(GLib.get_user_runtime_dir()) / "reolink-viewer" / "neolink.toml"

    def apply(self, cameras: list[Camera]) -> None:
        """Serve the battery cameras among these; restarts neolink if they changed."""
        old = {c.id: c for c in self._cameras}
        self._cameras = [c for c in cameras if c.on_demand and c.uid.strip()]
        for cam in self._cameras:
            prev = old.get(cam.id)
            if prev is None or (prev.username, prev.password, prev.uid) != (
                cam.username, cam.password, cam.uid,
            ):
                self._rejected.discard(cam.id)  # edited login: worth one more try
        self._sync(force=True)

    def ensure_running(self, cam: Camera) -> str | None:
        """Called before a battery tile connects. Returns why it can't, or None."""
        if not self.binary:
            return "neolink is not installed (run install.sh)"
        if cam.id in self._rejected:
            return "Login rejected: check the password (Edit…)"
        self._sync()
        return None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        self._config = ""
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._path.unlink(missing_ok=True)  # it holds passwords

    def _sync(self, force: bool = False) -> None:
        cameras = [c for c in self._cameras if c.id not in self._rejected]
        text = config_text(cameras) if cameras else ""
        running = self._proc is not None and self._proc.poll() is None
        if running and text == self._config:
            return
        if not running and not force and time.monotonic() - self._started < MIN_RESTART_INTERVAL_S:
            return
        self.stop()
        if not text or not self.binary:
            return
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
        self._config = text
        self._started = time.monotonic()
        threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            GLib.idle_add(self._on_line, line.rstrip())
        proc.wait()
        GLib.idle_add(self._on_exit, proc)

    def _on_line(self, line: str) -> bool:
        if not line.strip():
            return False
        names = {c.id: c.name for c in self._cameras}
        m = _LINE.match(line)
        level, text = (m["level"], m["text"]) if m else ("DEBUG", line)
        cam_id, _, rest = text.partition(": ")
        if cam_id in names:
            text = f"{names[cam_id]}: {rest}"
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
            self._config = ""
        return False
