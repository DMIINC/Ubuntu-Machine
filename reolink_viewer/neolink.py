"""Runs neolink, which serves Reolink battery cameras over RTSP on this computer.

Battery cameras sleep with every port closed and wake only for Reolink's own
protocol (Baichuan, over UDP). neolink (github.com/QuantumEntangledAndy/neolink)
finds them on the LAN by UID and logs in, which wakes them. Reolink's servers
are never contacted: discovery is a local broadcast only.

neolink runs only while a battery camera is being watched, streaming only what
is shown; stopping it disconnects, and the camera goes back to sleep. Its own
pause/resume (`pause.on_client` + `idle_disconnect`) was tried first, but after
an idle period it often sent nothing for 20-40 s.

Where a wake's time goes (neolink 0.6.3-rc.2 source, 2026-11): about 15 s until
neolink serves the stream (discovery, login, two fixed 2 s sleeps, the camera's
encoder). Then each RTSP connection costs two keyframe intervals, and a timing
bug in neolink often kills the first connection of a process: gst-rtsp-server
waits 20 s for it and answers 503, with every other client queued behind it
(neolink serves all clients from one thread). That made 40-45 s. The optional
preload library (data/neolink_shim.c) removes both; without it the viewer can
only avoid making things worse (one lens at a time, no abandoned requests).
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
# A lens waits for another lens that is connecting, but not longer than this:
# neolink answers one client at a time, and a second one only queues behind it.
GATE_MAX_S = 4.0
SHIM_NAME = "neolink_shim.so"
SHIM_LOADED = "neolink_shim: active"  # the library prints this when it's in place


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# For experiments on the real camera (see CLAUDE.md):
# wait this long after the stream is up before the first connection,
WAKE_SETTLE_S = _env_number("REOLINK_VIEWER_WAKE_SETTLE_MS", 0) / 1000
# and how much history neolink replays to a new client (its default is 3000 ms; not below 300).
NEOLINK_BUFFER_MS = int(_env_number("REOLINK_VIEWER_NEOLINK_BUFFER_MS", 0))

# neolink's own log lines that mark a wake's stages (the text after "<camera>: ").
_STAGES = (
    ("Local discovery success", "found"),
    ("TCP Discovery success", "found"),
    ("Connected and logged in", "logged in"),
    ("Available at", "stream up"),
    ("Connection Lost", "connection lost"),
)
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


def find_shim(binary: str | None) -> str | None:
    """The preload library that fixes neolink's slow first connection, if it was
    built (install.sh does that) and isn't switched off.
    REOLINK_VIEWER_NEOLINK_SHIM: 0 = don't use it, or the path of the library."""
    choice = os.environ.get("REOLINK_VIEWER_NEOLINK_SHIM", "")
    if choice == "0" or not binary:
        return None
    if choice not in ("", "1"):
        candidates = [Path(choice)]
    else:
        candidates = [
            Path(binary).resolve().parent / SHIM_NAME,
            Path(__file__).resolve().parent.parent / SHIM_NAME,
            Path.home() / ".local/share/reolink-viewer" / SHIM_NAME,
            Path("/usr/local/share/reolink-viewer") / SHIM_NAME,
        ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


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
            *_address(cam),
            f"channel_id = {max(cam.channel, 1) - 1}",
            'discovery = "local"',  # LAN broadcast only, never Reolink's servers
            f"stream = {_toml(STREAM_PATHS.get(stream, 'subStream'))}",
            # Until the stream format is known, refuse clients rather than serve
            # a test pattern the viewer would take for video.
            "use_splash = false",
            "push_notifications = false",  # these go through Google and Reolink
            "update_time = false",
            *([f"buffer_duration = {max(NEOLINK_BUFFER_MS, 300)}"] if NEOLINK_BUFFER_MS else []),
        ]
    return "\n".join(lines) + "\n"


def _address(cam: Camera) -> list[str]:
    """With the camera's IP as well as its UID, neolink sends its wake-up packets
    straight to the camera (ports 2015/2018) besides broadcasting them. After a
    longer idle the Duo ignored the broadcasts (2026-10-04), though it still
    answered ping, which Wi-Fi power saving delivers like any unicast."""
    host = cam.host.strip()
    return [f"address = {_toml(host)}"] if host else []


# Loaded here, not after the fork: the child of a process with many threads
# (GStreamer's) must not do more than make the call.
try:
    _prctl = ctypes.CDLL("libc.so.6", use_errno=True).prctl
except (OSError, AttributeError):
    _prctl = None


def _die_with_parent() -> None:
    # Runs in the child before exec: don't outlive the viewer if it crashes.
    if _prctl is not None:
        _prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


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
        self._started = 0.0  # when the running process was started
        self._all_ready_at = 0.0  # when the last of the served streams was mounted
        self._connecting: str | None = None  # id of the lens connecting right now
        self._connecting_since = 0.0
        self._failed_at: dict[str, float] = {}  # id -> when its last attempt failed
        self._timeline: dict[str, list[tuple[float, str]]] = {}  # id -> steps of its wake
        self._stage: dict[str, str] = {}  # id -> latest stage, for the tile's status text
        self._shim_off = False  # the preload library let a wake down in this run
        self._shim_used = False  # the running process was started with it
        self._shim_active = False  # ... and printed that it's in place
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
        for gone in set(self._awake) - set(self._cameras):
            del self._awake[gone]  # e.g. changed to another protocol while awake
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
        if self._connecting == cam.id:
            self._connecting = None
        self._timeline.pop(cam.id, None)
        self._stage.pop(cam.id, None)
        if self._awake.pop(cam.id, None) is not None:
            self._schedule_sync()

    def ready(self, cam: Camera) -> bool:
        """Whether to connect to this camera's stream now.

        Not before neolink serves the streams of all the cameras it's waking: a
        client that connected earlier, even right after its own stream was up
        while the other lens was still starting, often got no video for 20 s
        (then 503). And one lens at a time: neolink answers a single client at a
        time, so a second lens would only queue behind the first, and then start
        further behind live.
        """
        if self._proc is None or cam.id not in self._serving:
            return False
        if not self._ready >= self._serving.keys():
            return False
        now = time.monotonic()
        if now < self._all_ready_at + WAKE_SETTLE_S:
            return False
        other = self._connecting
        if other not in (None, cam.id) and other in self._awake:
            # The wait is limited, counted from the other lens's connect or from
            # this lens's own last failure if later: a lens stuck in a 20 s stall
            # must not hold this one up for ever, while after a 503 the lens that
            # was queued still goes first.
            waited_from = max(self._connecting_since, self._failed_at.get(cam.id, 0.0))
            if now - waited_from < GATE_MAX_S:
                return False
        return True

    def stage(self, cam: Camera) -> str:
        """Where a camera's wake has got to, for the tile ("found", "logged in", …)."""
        return self._stage.get(cam.id, "")

    def note(self, cam: Camera, what: str) -> None:
        """A tile reports a step of waking its camera: "wake" starts a timeline,
        "first frame" or "gave up…" ends it and logs it as one line."""
        now = time.monotonic()
        if what == "wake":
            self._timeline[cam.id] = [(now, "double-click")]
            self._stage.pop(cam.id, None)
            if self._proc is not None and cam.id in self._serving:
                self._timeline[cam.id].append((now, "neolink already running"))
            return
        steps = self._timeline.get(cam.id)
        if steps is None:
            return
        steps.append((now, what))
        if what.startswith("connect#"):
            self._connecting, self._connecting_since = cam.id, now
        elif self._connecting == cam.id and (
            what in ("play ok", "first frame") or what.startswith(("failed", "gave up", "no video"))
        ):
            self._connecting = None
        if what.startswith(("failed", "no video")):
            self._failed_at[cam.id] = now
        if what == "first frame" or what.startswith("gave up"):
            start = steps[0][0]
            log.info("[%s] wake%s: %s s", cam.name, " (with speed-up)" if self._shim_active else "",
                     " · ".join(f"{text} {moment - start:.1f}" for moment, text in steps[1:]))
            del self._timeline[cam.id]
            if what.startswith("gave up") and cam.id in self._ready:
                self._wake_failed_with_stream_up()

    def _wake_failed_with_stream_up(self) -> None:
        # neolink was serving the stream and still no picture came. If the preload
        # library is in use it is the first suspect: it has only been tried in
        # simulation. Do without it for the rest of this run.
        if self._shim_active and not self._shim_off:
            self._shim_off = True
            log.warning("battery camera gave no video with the wake speed-up: "
                        "switching it off until the viewer is restarted")
            self._serving = {}  # restart neolink without it on the next wake

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        self._serving = {}
        self._ready.clear()
        self._connecting = None
        self._shim_active = False
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc and proc.stdout:
            proc.stdout.close()
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
        shim = None if self._shim_off else find_shim(self.binary)
        self._shim_used = bool(shim)
        log.info("starting neolink for %s%s", ", ".join(c.name for c in cameras),
                 " (with the wake speed-up)" if shim else "")
        self._proc = subprocess.Popen(
            [self.binary, "rtsp", "--config", str(self._path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            env=_environment(shim),
            preexec_fn=_die_with_parent,
        )
        self._serving = wanted
        self._started = time.monotonic()
        for cam in cameras:
            if cam.id in self._timeline:
                self._timeline[cam.id].append((self._started, "neolink started"))
        threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()
        return False

    def _read(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:
                GLib.idle_add(self._on_line, line.rstrip())
        except ValueError:
            pass  # stop() closed the pipe
        proc.wait()
        GLib.idle_add(self._on_exit, proc)

    def _on_line(self, line: str) -> bool:
        if not line.strip():
            return False
        if SHIM_LOADED in line:
            self._shim_active = True
            log.info("neolink: wake speed-up library is active")
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
            if self._ready >= self._serving.keys():
                self._all_ready_at = time.monotonic()
        if cam_id in names:
            for marker, stage in _STAGES:
                if rest.startswith(marker):
                    self._stage[cam_id] = stage
                    if cam_id in self._timeline:
                        self._timeline[cam_id].append((time.monotonic(), stage))
        for cam in self._cameras.values():
            uid = cam.uid.strip()
            if uid and uid in text:
                text = text.replace(uid, uid[:4] + "…")  # the UID opens the camera: keep it out of logs
        if LOGIN_REJECTED in text and cam_id in names:
            self._rejected.add(cam_id)
            self._on_login_rejected(cam_id)
        if level == "ERROR":
            log.warning("neolink: %s", text)
        elif level in ("WARN", "INFO") and m:
            log.info("neolink: %s", text)
        else:
            log.debug("neolink: %s", text)  # e.g. GStreamer's own lines
        return False

    def _on_exit(self, proc: subprocess.Popen) -> bool:
        if proc is self._proc:  # not stopped by us
            log.warning("neolink exited (code %s)", proc.returncode)
            served = self._serving.keys()
            if (self._shim_used and not self._shim_off and not served & self._rejected
                    and time.monotonic() - self._started < 30):
                # Never seen, but if neolink dies young with the library loaded, the
                # library (tried only in simulation so far) is the first suspect.
                self._shim_off = True
                log.warning("neolink exited while using the wake speed-up: switching it off "
                            "until the viewer is restarted")
            self._proc = None
            self._serving = {}
            self._ready.clear()
            self._connecting = None
            self._shim_active = False
            self._crashed = time.monotonic()
        return False


def _environment(shim: str | None) -> dict[str, str]:
    """neolink's environment: the preload library, and with -v the log lines that
    show how its RTSP server handles each connection (they arrive in the viewer's
    debug log and tell a stalled first connection apart from a slow camera)."""
    env = dict(os.environ)
    if shim:
        env["LD_PRELOAD"] = f"{shim}:{env['LD_PRELOAD']}" if env.get("LD_PRELOAD") else shim
    if log.isEnabledFor(logging.DEBUG):
        env.setdefault("RUST_LOG", "info,neolink::rtsp=debug")
        env.setdefault("GST_DEBUG", "basesrc:4,rtspmedia:4,rtspclient:2")
        env.setdefault("GST_DEBUG_NO_COLOR", "1")
    return env
