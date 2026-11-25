#!/usr/bin/python3.12
"""Fake neolink: stands in for `neolink rtsp --config FILE` in Reolink Viewer tests.

It is NOT neolink. It imitates what the viewer can see of neolink 0.6.3-rc.2:
the log lines (same text and order as the real source prints at INFO), the RTSP
mount points, and gst-rtsp-server's behaviour (it uses the same library with the
same settings neolink uses: one media per client, default thread pool).

usage (the viewer runs it itself):
    REOLINK_VIEWER_NEOLINK=/path/to/fake_neolink.py  reolink-viewer ...
    -> the viewer executes:  fake_neolink.py rtsp --config $XDG_RUNTIME_DIR/reolink-viewer/neolink.toml
by hand:
    fake_neolink.py rtsp --config FILE        (FILE as written by reolink_viewer.neolink.config_text)

It parses the TOML (bind, bind_port, [[cameras]] name/uid/address/channel_id/stream/
use_splash), then for every camera plays this timeline and prints neolink's lines:
    0 s                 "<name>: Connecting to camera at Address: A, UID: U", "Trying TCP discovery",
                        "Trying local discovery"
    DISCOVERY_S         "<name>: Local discovery success U at A:port", "<name>: Logging in"
                        (every 15 s without success: "Connection Lost" + "Attempt reconnect", as neolink)
    + LOGIN_S           "<name>: Connected and logged in"
    + POSTLOGIN_S/2     "<name>: Camera time is already set: ..."      (neolink sleeps 2 s + 2 s here)
    + POSTLOGIN_S/2     "<name>: Model ...", "<name>: Firmware Version ..."; the "camera" starts sending frames
    + STREAM_S          "<name>: Available at /<name>/main, /<name>/Main, /<name>/mainStream, ..."
Before "Available" the mounts exist but answer 400 (neolink's dummy factory with use_splash = false);
with use_splash = true they serve a test pattern, as neolink does.
After it, /<name>/mainStream (or subStream, per the config's `stream`) serves H.264 video of a moving
test pattern with the camera name and a clock, through appsrc ! queue ! h264parse ! rtph264pay like
neolink's media, fed "history first, then live" starting at a keyframe.
It exits with code 0 on SIGTERM / SIGINT.

Behaviour is selected by environment variables (all optional; times in seconds, floats):
  timeline
    FAKE_NEOLINK_DISCOVERY_S   5     start -> local discovery answered  ("a,b" = per camera, in config order)
    FAKE_NEOLINK_LOGIN_S       1.5   discovery -> logged in
    FAKE_NEOLINK_POSTLOGIN_S   4     neolink's two fixed 2 s sleeps after login
    FAKE_NEOLINK_STREAM_S      4     camera usable -> "Available at"  ("a,b" = per camera)
    FAKE_NEOLINK_STAGGER_S     1     every further camera gets "Available" this much later than the one before
    FAKE_NEOLINK_SCALE         1     multiplies all of the above (0.2 = quick runs)
    FAKE_NEOLINK_PHASE_S       0     delay of a camera's first frame, i.e. its keyframe phase ("a,b" = per camera;
                                     not scaled)
    FAKE_NEOLINK_REJECT        ""    camera name or index whose login is rejected: prints
                                     "<name>: Login credentials were not accepted" and exits 1, as neolink
  simulated failures (HYPOTHESES about neolink, see research/neolink-wake-client.md). A "stalled" media
  gets no data, so gst-rtsp-server holds its DESCRIBE for 20 s and then answers 503:
    FAKE_NEOLINK_STALL_FIRST   0     the first N media built in this process stall
    FAKE_NEOLINK_STALL_EARLY_S 0     a media built less than this long after its camera's "Available" stalls
    FAKE_NEOLINK_STALL_BURST_S 0     a media built less than this long after the previous media was built stalls
    FAKE_NEOLINK_STALL_UNTIL_ALL 0   1 = a media built before every camera is "Available" stalls
    FAKE_NEOLINK_STALL_P       0     each media stalls with this probability (0..1), seed FAKE_NEOLINK_SEED
    FAKE_NEOLINK_FEED_DELAY_MS -1    >= 0: start feeding the appsrc this long after the media was built,
                                     and set the appsrc alone to PAUSED first like neolink does (race tests);
                                     -1 = feed only once the media pipeline itself has reached PAUSED (never races)
  server
    FAKE_NEOLINK_THREADS       ""    gst-rtsp-server client threads; empty = library default (1), as neolink
    FAKE_NEOLINK_PREROLL_S     0     extra delay before a healthy media's first frame (slower preroll)
  video
    FAKE_NEOLINK_SIZE 640x360, FAKE_NEOLINK_FPS 15, FAKE_NEOLINK_GOP_S 2 (keyframe interval),
    FAKE_NEOLINK_BITRATE 600 (kbit/s), FAKE_NEOLINK_MAX_BYTES 1048576 (appsrc/queue size; neolink
    uses 2 s of the camera's NOMINAL bitrate), FAKE_NEOLINK_BUFFER_S 3 (history replayed to a new
    client; neolink's buffer_duration default)
Lines that do not look like neolink's ("fake-neolink: ...") say what the fake itself did, e.g. which
media it built and whether it let it stall; the viewer logs those at DEBUG.

Recipes
  (a) healthy start                      no variables
  (b) first connection never prerolls, the next one works
                                         FAKE_NEOLINK_STALL_FIRST=1
      the same through the real mechanism (the feeder pushes before gst-rtsp-server has the media
      PAUSED; every media whose history holds a keyframe then dies, so make keyframes rarer than the
      3 s history to get working retries)
                                         FAKE_NEOLINK_FEED_DELAY_MS=0 FAKE_NEOLINK_GOP_S=4
  (c) slow discovery                     FAKE_NEOLINK_DISCOVERY_S=22   (one 15 s attempt fails first)
  quick timeline ("Available" after ~3 s and ~4 s)
      FAKE_NEOLINK_DISCOVERY_S=1 FAKE_NEOLINK_LOGIN_S=0.3 FAKE_NEOLINK_POSTLOGIN_S=0.4 FAKE_NEOLINK_STREAM_S=1.3

Needs /usr/bin/python3.12 with PyGObject, the GstRtspServer typelib (gir1.2-gst-rtsp-server-1.0) and
x264enc. The port comes from the TOML: the viewer hard-codes 18554 (config.py NEOLINK_PORT), so a test
harness that needs another port has to patch reolink_viewer.config.NEOLINK_PORT before importing
reolink_viewer.neolink (see scratchpad/agent-4/run_viewer.py).
"""
from __future__ import annotations

import collections
import datetime
import os
import queue
import random
import signal
import sys
import threading
import time
import tomllib

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtspServer  # noqa: E402

VERSION = "0.6.3-rc.2"
T0 = time.monotonic()


def env_f(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


def env_list(name: str, default: float, index: int) -> float:
    """A float, or the index-th of a comma list (the last one for further cameras)."""
    parts = [p for p in (os.environ.get(name) or "").split(",") if p.strip()]
    if not parts:
        return default
    return float(parts[min(index, len(parts) - 1)])


SCALE = env_f("FAKE_NEOLINK_SCALE", 1.0)
STALL_FIRST = int(env_f("FAKE_NEOLINK_STALL_FIRST", 0))
STALL_EARLY_S = env_f("FAKE_NEOLINK_STALL_EARLY_S", 0)
STALL_BURST_S = env_f("FAKE_NEOLINK_STALL_BURST_S", 0)
STALL_UNTIL_ALL = os.environ.get("FAKE_NEOLINK_STALL_UNTIL_ALL", "0") not in ("", "0")
STALL_P = env_f("FAKE_NEOLINK_STALL_P", 0)
FEED_DELAY_MS = env_f("FAKE_NEOLINK_FEED_DELAY_MS", -1)
PREROLL_S = env_f("FAKE_NEOLINK_PREROLL_S", 0)
WIDTH, HEIGHT = (int(v) for v in os.environ.get("FAKE_NEOLINK_SIZE", "640x360").split("x"))
FPS = int(env_f("FAKE_NEOLINK_FPS", 15))
GOP_S = env_f("FAKE_NEOLINK_GOP_S", 2)
BUFFER_S = env_f("FAKE_NEOLINK_BUFFER_S", 3)
BITRATE_KBPS = int(env_f("FAKE_NEOLINK_BITRATE", 600))
MAX_BYTES = int(env_f("FAKE_NEOLINK_MAX_BYTES", 1048576))
RNG = random.Random(int(env_f("FAKE_NEOLINK_SEED", 1)))

_print_lock = threading.Lock()


def out(line: str) -> None:
    with _print_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def log(level: str, module: str, text: str) -> None:
    """One line in env_logger's default format, which is what neolink prints."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out(f"[{stamp} {level:<5} {module}] {text}")


def note(text: str) -> None:
    """The fake's own remarks (not neolink's format): the viewer logs them at DEBUG."""
    out(f"fake-neolink: +{time.monotonic() - T0:.2f}s {text}")


class Frame:
    __slots__ = ("data", "keyframe", "ts")

    def __init__(self, data: bytes, keyframe: bool, ts: float):
        self.data, self.keyframe, self.ts = data, keyframe, ts


class FakeCamera:
    """One [[cameras]] entry: its timeline, its encoder ("the camera") and its mounts."""

    def __init__(self, index: int, conf: dict, server: "Server"):
        self.index = index
        self.server = server
        self.name = conf["name"]
        self.uid = conf.get("uid", "")
        self.address = conf.get("address", "")
        self.channel = int(conf.get("channel_id", 0))
        self.use_splash = bool(conf.get("use_splash", False))
        kind = conf.get("stream", "both")
        self.kind = "sub" if kind == "subStream" else "main"
        self.available_at: float | None = None
        self.history: collections.deque[Frame] = collections.deque()
        self.listeners: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.frames = 0
        self.encoder: Gst.Element | None = None

    # ---- mounts --------------------------------------------------------
    def paths(self) -> list[str]:
        n = self.name
        if self.kind == "main":  # src/rtsp/mod.rs:339-349
            return [f"/{n}/main", f"/{n}/Main", f"/{n}/mainStream", f"/{n}/MainStream",
                    f"/{n}/Mainstream", f"/{n}/mainstream", f"/{n}"]
        return [f"/{n}/sub", f"/{n}/Sub", f"/{n}/subStream", f"/{n}/SubStream",  # mod.rs:369-381
                f"/{n}/Substream", f"/{n}/substream", f"/{n}"]

    def mount(self, factory: GstRtspServer.RTSPMediaFactory) -> None:
        mounts = self.server.rtsp.get_mount_points()
        for path in self.paths():
            mounts.add_factory(path, factory)

    # ---- timeline ------------------------------------------------------
    def start(self) -> None:
        log("INFO", "neolink::rtsp", f"{self.name}: Rtsp Starting")
        self.mount(DummyFactory(self))
        self._connect()
        i = self.index
        discovery = env_list("FAKE_NEOLINK_DISCOVERY_S", 5, i) * SCALE
        # neolink gives up a discovery attempt after 15 s and starts over.
        attempt = 15.0
        t = attempt
        while t < discovery:
            GLib.timeout_add(int(t * 1000), self._discovery_timeout)
            t += attempt
        GLib.timeout_add(int(discovery * 1000), self._discovered)

    def _where(self) -> str:
        if self.address and self.uid:
            return f"Address: {self.address}, UID: {self.uid}"
        return f"Address: {self.address}" if self.address else f"UID: {self.uid}"

    def _connect(self) -> None:
        log("INFO", "neolink::utils", f"{self.name}: Connecting to camera at {self._where()}")
        if self.address:
            log("INFO", "neolink_core::bc_protocol", f"{self.name}: Trying TCP discovery")
        log("INFO", "neolink_core::bc_protocol", f"{self.name}: Trying local discovery")

    def _discovery_timeout(self) -> bool:
        log("WARN", "neolink::common::camthread",
            f"{self.name}: Connection Lost: Failed to connect to camera {self.name} at "
            f"{self._where()} on channel {self.channel}")
        out("")
        out("Caused by:")
        out("    Timeout while waiting for camera discovery")  # multi-line, like anyhow's {:?}
        log("INFO", "neolink::common::camthread", f"{self.name}: Attempt reconnect in 50ms")
        self._connect()
        return False

    def _discovered(self) -> bool:
        ip = self.address.split(":")[0] or "192.168.1.35"
        log("INFO", "neolink_core::bc_protocol",
            f"{self.name}: Local discovery success {self.uid} at {ip}:{40000 + self.index}")
        log("INFO", "neolink::utils", f"{self.name}: Logging in")
        GLib.timeout_add(int(env_list("FAKE_NEOLINK_LOGIN_S", 1.5, self.index) * SCALE * 1000),
                         self._logged_in)
        return False

    def _logged_in(self) -> bool:
        reject = os.environ.get("FAKE_NEOLINK_REJECT", "")
        if reject and reject in (self.name, str(self.index)):
            log("ERROR", "neolink::common::camthread",
                f"{self.name}: Login credentials were not accepted")
            self.server.quit(1)
            return False
        log("INFO", "neolink::utils", f"{self.name}: Connected and logged in")
        half = int(env_f("FAKE_NEOLINK_POSTLOGIN_S", 4) * SCALE * 500)
        GLib.timeout_add(half, self._time_checked)
        GLib.timeout_add(2 * half, self._usable)
        return False

    def _time_checked(self) -> bool:
        log("INFO", "neolink::common::camthread",
            f"{self.name}: Camera time is already set: 2026-10-04 9:10:00.0 +00:00:00")
        return False

    def _usable(self) -> bool:
        log("INFO", "neolink::common::neocam", f"{self.name}: Model Reolink Duo (fake)")
        log("INFO", "neolink::common::neocam", f"{self.name}: Firmware Version v3.0.0.1045_2206131045")
        # The camera's first frame (a keyframe) comes PHASE_S after it became usable.
        phase = env_list("FAKE_NEOLINK_PHASE_S", 0, self.index)
        GLib.timeout_add(int(phase * 1000), lambda: self._start_encoder() and False)
        delay = (env_list("FAKE_NEOLINK_STREAM_S", 4, self.index)
                 + self.index * env_f("FAKE_NEOLINK_STAGGER_S", 1)) * SCALE
        GLib.timeout_add(int(max(delay, phase + 0.3) * 1000), self._available)
        return False

    def _available(self) -> bool:
        self.mount(StreamFactory(self))  # replaces the dummy factory on every path
        self.available_at = time.monotonic()
        log("INFO", "neolink::rtsp::stream", f"{self.name}: Available at {', '.join(self.paths())}")
        return False

    # ---- "the camera": a live H.264 encoder ----------------------------
    def _start_encoder(self) -> None:
        pattern = ("ball", "smpte", "pinwheel", "snow")[self.index % 4]
        gop = max(1, int(round(GOP_S * FPS)))
        desc = (
            f"videotestsrc is-live=true pattern={pattern} "
            f"! video/x-raw,width={WIDTH},height={HEIGHT},framerate={FPS}/1 "
            f"! timeoverlay ! textoverlay text=\"{self.name} ch{self.channel}\" valignment=bottom "
            "! videoconvert ! video/x-raw,format=I420 "
            f"! x264enc tune=zerolatency speed-preset=ultrafast key-int-max={gop} bitrate={BITRATE_KBPS} "
            "! video/x-h264,stream-format=byte-stream,alignment=au "
            "! appsink name=out emit-signals=true sync=false max-buffers=30 drop=true"
        )
        self.encoder = Gst.parse_launch(desc)
        self.encoder.get_by_name("out").connect("new-sample", self._on_sample)
        self.encoder.set_state(Gst.State.PLAYING)

    def _on_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(info.data)
        buf.unmap(info)
        keyframe = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        # Like neolink: its own timestamps, one frame interval apart (streamthread.rs:416-424).
        frame = Frame(data, keyframe, self.frames / FPS)
        self.frames += 1
        with self.lock:
            self.history.append(frame)
            while self.history and self.history[0].ts < frame.ts - BUFFER_S:  # streamthread.rs:596-602
                self.history.popleft()
            for q in self.listeners:
                q.put(frame)
        return Gst.FlowReturn.OK

    def subscribe(self) -> tuple[list[Frame], queue.Queue]:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.listeners.append(q)
            return list(self.history), q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)


class DummyFactory(GstRtspServer.RTSPMediaFactory):
    """neolink's factory before the stream format is known (src/rtsp/factory.rs:21-35)."""

    def __init__(self, cam: FakeCamera):
        super().__init__()
        self.cam = cam
        self.set_shared(False)

    def do_create_element(self, _url):
        if not self.cam.use_splash:
            note(f"{self.cam.name}: client before Available -> no media (400)")
            return None
        note(f"{self.cam.name}: client before Available -> splash")
        return Gst.parse_launch(
            "( videotestsrc pattern=snow num-buffers=500 ! video/x-raw,format=YUY2,width=896,height=512,"
            "framerate=25/1 ! textoverlay text=\"Stream not Ready\" valignment=top halignment=left "
            "! jpegenc ! rtpjpegpay name=pay0 )")


class StreamFactory(GstRtspServer.RTSPMediaFactory):
    """neolink's real factory: one media per client, an appsrc fed with the camera's frames."""

    def __init__(self, cam: FakeCamera):
        super().__init__()
        self.cam = cam
        self.set_shared(False)  # src/rtsp/gst/factory.rs:38
        self.set_eos_shutdown(False)
        self.set_stop_on_disconnect(False)
        self.set_suspend_mode(GstRtspServer.RTSPSuspendMode.RESET)
        self.set_transport_mode(GstRtspServer.RTSPTransportMode.PLAY)

    def do_create_element(self, _url):
        server = self.cam.server
        now = time.monotonic()
        with server.lock:
            server.media_count += 1
            number = server.media_count
            since_prev = now - server.last_media if server.last_media else None
            server.last_media = now
        since_available = now - (self.cam.available_at or now)
        why = None
        if number <= STALL_FIRST:
            why = f"STALL_FIRST={STALL_FIRST}"
        elif since_available < STALL_EARLY_S:
            why = f"{since_available:.2f}s after Available < STALL_EARLY_S={STALL_EARLY_S:g}"
        elif since_prev is not None and since_prev < STALL_BURST_S:
            why = f"{since_prev:.2f}s after the previous media < STALL_BURST_S={STALL_BURST_S:g}"
        elif STALL_UNTIL_ALL and not server.all_available():
            why = "not every camera is Available yet"
        elif STALL_P and RNG.random() < STALL_P:
            why = f"STALL_P={STALL_P:g}"
        element = Gst.parse_launch(
            "( appsrc name=vidsrc is-live=false block=false emit-signals=true do-timestamp=false "
            f"stream-type=seekable format=time max-bytes={MAX_BYTES} "
            f"! queue name=source_queue max-size-bytes={MAX_BYTES} max-size-buffers=0 max-size-time=5000000000 "
            "! h264parse ! rtph264pay name=pay0 pt=96 )")
        appsrc = element.get_by_name("vidsrc")
        # neolink: stream-type Seekable plus a seek callback that always says yes (factory.rs:55-59).
        appsrc.connect("seek-data", lambda _src, _offset: True)
        note(f"{self.cam.name}: media #{number} built, {since_available:.2f}s after Available"
             + (f" -> STALLED ({why}): no data, 503 after 20 s" if why else ""))
        if why is None:
            threading.Thread(target=feed, args=(self.cam, appsrc, element, number), daemon=True).start()
        return element


def feed(cam: FakeCamera, appsrc, element, number: int) -> None:
    """Like neolink's send_to_appsrc (src/rtsp/stream.rs:794-894): history from its first
    keyframe, then live frames, paced by frame time, timestamps starting at 0."""
    if FEED_DELAY_MS >= 0:
        # neolink's order: the feeder starts as soon as the factory callback has run, sets the
        # appsrc (alone) to PAUSED and pushes, whatever state the media is in.
        time.sleep(FEED_DELAY_MS / 1000)
        appsrc.set_state(Gst.State.PAUSED)
    else:
        # Wait until gst-rtsp-server has set the media's elements to PAUSED: the queue's
        # pads are then active, so the first buffer cannot be refused.
        deadline = time.monotonic() + 25
        q_el = element.get_by_name("source_queue")
        while time.monotonic() < deadline and q_el.get_state(0)[1] < Gst.State.PAUSED:
            time.sleep(0.005)
    if PREROLL_S:
        time.sleep(PREROLL_S)
    history, live = cam.subscribe()
    pushed = flushing = 0
    paused = True  # neolink's own idea of the appsrc state (stream.rs:801)
    try:
        ts0 = None
        prev = None
        found_key = False  # wait_for_keyframe: only the very start is skipped (stream.rs:521-535)
        wait_for_iframe = True  # the pushing side: again after every refused push (stream.rs:812-816, 866)
        frames = iter(history)
        while True:
            frame = next(frames, None)
            if frame is None:
                try:
                    frame = live.get(timeout=1.0)
                except queue.Empty:
                    if appsrc.get_parent() is None:
                        break
                    continue
            if not found_key:
                if not frame.keyframe:
                    continue
                found_key = True
            if ts0 is None:
                ts0 = prev = frame.ts
            # frametime_stream (stream.rs:616-634): every frame is paced, also those skipped below.
            wait = min(max(frame.ts - prev, 0.0), 1.0 / FPS)
            prev = frame.ts
            if wait:
                time.sleep(wait)
            if appsrc.get_parent() is None:  # media was freed (check_live, stream.rs:498-505)
                break
            if wait_for_iframe:
                if not frame.keyframe:
                    continue
                wait_for_iframe = False
            buf = Gst.Buffer.new_wrapped(frame.data)
            buf.pts = buf.dts = int((frame.ts - ts0) * Gst.SECOND)
            if not frame.keyframe:
                buf.set_flags(Gst.BufferFlags.DELTA_UNIT)
            ret = appsrc.emit("push-buffer", buf)
            if ret == Gst.FlowReturn.FLUSHING:
                flushing += 1
                if flushing == 1:
                    log("INFO", "neolink::rtsp::stream", "Buffer full on vidsrc")  # stream.rs:867
                wait_for_iframe = True
                if flushing > 40 * FPS:  # the media is gone for good
                    break
            elif ret != Gst.FlowReturn.OK:
                break
            else:
                pushed += 1
                if pushed == 1:
                    note(f"{cam.name}: media #{number} first frame pushed")
            if FEED_DELAY_MS >= 0:
                # neolink switches the appsrc ALONE between PLAYING and PAUSED by its fill
                # level (stream.rs:872-878), whatever the media pipeline is doing.
                level = appsrc.get_property("current-level-bytes")
                if level >= MAX_BYTES * 2 // 3 and paused:
                    note(f"{cam.name}: media #{number} appsrc {level} bytes queued -> PLAYING")
                    appsrc.set_state(Gst.State.PLAYING)
                    paused = False
                elif level <= MAX_BYTES // 3 and not paused:
                    note(f"{cam.name}: media #{number} appsrc drained -> PAUSED")
                    appsrc.set_state(Gst.State.PAUSED)
                    paused = True
    finally:
        cam.unsubscribe(live)
        note(f"{cam.name}: media #{number} feeder ended ({pushed} frames)")


class Server:
    def __init__(self, conf: dict):
        self.lock = threading.Lock()
        self.media_count = 0
        self.last_media = 0.0
        self.exit_code = 0
        self.loop = GLib.MainLoop()
        self.rtsp = GstRtspServer.RTSPServer()
        self.rtsp.set_address(conf.get("bind", "0.0.0.0"))
        self.rtsp.set_service(str(conf.get("bind_port", 8554)))
        threads = os.environ.get("FAKE_NEOLINK_THREADS", "")
        if threads:
            self.rtsp.get_thread_pool().set_max_threads(int(threads))
        self.rtsp.connect("client-connected", self._on_client)
        self.cameras = [FakeCamera(i, c, self) for i, c in enumerate(conf.get("cameras", []))
                        if c.get("enabled", True)]
        self.bind = (conf.get("bind", "0.0.0.0"), conf.get("bind_port", 8554))

    def _on_client(self, _server, client) -> None:
        client.connect("new-session", lambda _c, session: session.set_timeout(30))  # gst/server.rs:60-70

    def all_available(self) -> bool:
        return all(c.available_at is not None for c in self.cameras)

    def run(self) -> int:
        log("INFO", "neolink", f"Neolink {VERSION} release (FAKE: scratchpad/harness/fake_neolink.py)")
        for cam in self.cameras:
            cam.start()
        log("INFO", "neolink::rtsp", f"Starting RTSP Server at {self.bind[0]}:{self.bind[1]}")
        if not self.rtsp.attach(None):
            log("ERROR", "neolink::rtsp", f"Error: could not bind {self.bind[0]}:{self.bind[1]}")
            return 1
        note(f"client threads: {self.rtsp.get_thread_pool().get_max_threads()}, "
             f"gst {Gst.version_string()}")
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal)
        self.loop.run()
        return self.exit_code

    def _on_signal(self) -> bool:
        self.quit(0)
        return False

    def quit(self, code: int) -> None:
        self.exit_code = code
        # Don't wait for streaming threads or a blocked client handler: neolink exits at once.
        threading.Timer(0.3, os._exit, args=(code,)).start()
        self.loop.quit()


def main(argv: list[str]) -> int:
    args = argv[1:]
    if "--version" in args:
        print(f"neolink {VERSION} (fake)")
        return 0
    path = None
    for i, arg in enumerate(args):
        if arg == "--config" and i + 1 < len(args):
            path = args[i + 1]
        elif arg.startswith("--config="):
            path = arg.split("=", 1)[1]
    if not args or args[0] != "rtsp" or not path:
        sys.stderr.write("usage: fake_neolink.py rtsp --config FILE   (see the docstring)\n")
        return 2
    with open(path, "rb") as fh:
        conf = tomllib.load(fh)
    Gst.init(None)
    code = Server(conf).run()
    sys.stdout.flush()
    os._exit(code)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
