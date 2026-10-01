"""A GTK widget that plays one camera stream through GStreamer, with auto-reconnect."""

from __future__ import annotations

import logging
import os
import threading
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gdk, GLib, Gst, GstVideo, Gtk  # noqa: E402

from .config import Camera  # noqa: E402

log = logging.getLogger(__name__)

RTSP_LATENCY_MS = 300
RETRY_MIN_S = 2
RETRY_MAX_S = 30
STALL_TIMEOUT_S = 20
WATCHDOG_INTERVAL_S = 5


# Video output paths, best first. Each one that fails is skipped from then on:
#   gl      - gtkglsink: GPU converts, scales and draws; decoders hand it frames in
#             GPU memory (on Wayland this needs GST_GL_API=opengl3, see display.py)
#   wayland - gtkwaylandsink: frames go to the compositor as a subsurface, which
#             converts and scales them on the GPU (Wayland sessions only). Frames
#             reach it in system RAM, and it stalls if a frame callback goes missing.
#   cuda - NVIDIA GPU converts + scales to the window size, gtksink draws the result
#   cpu  - multithreaded convert + scale to the window size, gtksink draws
# REOLINK_VIEWER_SINK=wayland|gl|cuda|cpu forces a path ("sw" is an alias for cpu).
SINK_ORDER = ("gl", "wayland", "cuda", "cpu")
SINK_MODE = os.environ.get("REOLINK_VIEWER_SINK", "auto").lower().replace("sw", "cpu")
_failed_kinds: set[str] = set()

# GPU-only mode: software video decoders are disabled and the CPU conversion /
# drawing paths are never used. Set by the app from its settings.
GPU_ONLY = False
CPU_KINDS = ("cuda", "cpu")  # both paint the final frame with the CPU (cairo)
GPU_KINDS = ("gl", "wayland")  # both are tied to the widget's window surface


def configure(gpu_only: bool) -> None:
    """Apply GPU-only mode. Call once after Gst.init(), before creating tiles."""
    global GPU_ONLY
    GPU_ONLY = gpu_only
    for factory in Gst.Registry.get().get_feature_list(Gst.ElementFactory):
        klass = factory.get_metadata("klass") or ""
        if "Decoder/Video" in klass and "Hardware" not in klass:
            if gpu_only:
                factory.set_rank(Gst.Rank.NONE)
    if gpu_only:
        log.info("GPU-only mode: software video decoders disabled")


_cpu_sample = (time.monotonic(), time.process_time())
_cpu_percent = 0.0


def process_cpu_percent() -> float:
    """CPU used by this whole app recently, as % of one core (100% = one full core)."""
    global _cpu_sample, _cpu_percent
    now = (time.monotonic(), time.process_time())
    wall = now[0] - _cpu_sample[0]
    if wall >= 1.0:  # several tiles ask each interval; measure once
        _cpu_percent = 100.0 * (now[1] - _cpu_sample[1]) / wall
        _cpu_sample = now
    return _cpu_percent


# Decoded frames wait here for the pacer. If drawing or pacing ever falls far
# behind, the oldest frames are thrown away instead of piling up.
# Room for the pacer's largest cushion (0.8 s) plus a full burst on top at 30 fps.
LEAKY_QUEUE = "queue name=q max-size-buffers=50 max-size-bytes=0 max-size-time=0 leaky=downstream"
# REOLINK_VIEWER_SYNC=0 disables pacing: frames show the instant they're decoded.
PACED = os.environ.get("REOLINK_VIEWER_SYNC", "1") != "0"


class FramePacer:
    """Evens out frame display timing for cameras that send frames in bursts.

    Camera timestamps can't be trusted (Reolink's run fast/slow and arrive in
    bursts), so this ignores them. It learns the real frame interval from arrival
    times, keeps a small cushion of frames queued, and releases one frame per
    interval from the queue's own thread. The release rate is nudged up or down
    by up to 10% to hold the cushion steady, so lag never creeps up.
    """

    # The cushion (frames held back) adapts to the longest recent pause between
    # frame arrivals, so it's as small as the camera allows.
    MIN_CUSHION = 0.10
    MAX_CUSHION = 0.80
    EXTRA_LAG = 0.50  # frames later than cushion + this are dropped to get back to live

    def __init__(self, queue: Gst.Element):
        self.dropped = 0
        self._interval: float | None = None
        self._last_arrival: float | None = None
        self._arrivals: dict[int, float] = {}
        self._next: float | None = None
        self._gap_peak = 0.0
        self._wake = threading.Event()
        queue.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_arrive)
        queue.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._on_release)

    def reset(self) -> None:
        """Forget the schedule (on (re)connect); the learned frame rate is kept."""
        self._next = None
        self._last_arrival = None
        self._arrivals.clear()
        self._wake.clear()

    @property
    def cushion(self) -> float:
        return min(self.MAX_CUSHION, max(self.MIN_CUSHION, 1.2 * self._gap_peak))

    def interrupt(self) -> None:
        """Cut short any wait so the pipeline can stop promptly."""
        self._wake.set()

    def _on_arrive(self, _pad, info):
        now = time.monotonic()
        if self._last_arrival is not None:
            gap = now - self._last_arrival
            if gap < 1.0:  # ignore reconnect gaps; bursts and pauses average out
                self._interval = gap if self._interval is None else 0.98 * self._interval + 0.02 * gap
                # Remember the longest pause, fading over roughly a minute.
                self._gap_peak = max(gap, self._gap_peak * 0.998)
        self._last_arrival = now
        if len(self._arrivals) > 200:
            self._arrivals.clear()
        self._arrivals[info.get_buffer().pts] = now
        return Gst.PadProbeReturn.OK

    def _on_release(self, _pad, info):
        now = time.monotonic()
        arrived = self._arrivals.pop(info.get_buffer().pts, now)
        cushion = self.cushion
        if now - arrived > cushion + self.EXTRA_LAG:
            self._next = None
            self.dropped += 1
            return Gst.PadProbeReturn.DROP
        interval = min(max(self._interval or 0.04, 0.01), 0.5)
        if self._next is None or self._next < now - interval:
            # First frame, or we ran dry: restart the schedule with a fresh cushion.
            target = max(now, arrived + cushion)
        else:
            target = self._next
        if target > now and not self._wake.is_set():
            self._wake.wait(target - now)
        lag = target - arrived
        speedup = max(-0.1, min(0.1, 0.1 * (lag - cushion) / cushion))
        self._next = target + interval * (1 - speedup)
        return Gst.PadProbeReturn.OK


class VideoSink:
    """The bin handed to playbin plus the GTK widget that shows its frames."""

    def __init__(
        self, kind: str, bin_: Gst.Bin, caps_prefix: str | None = None,
        gtk_sink: Gst.Element | None = None,
    ):
        self.kind = kind
        self.element = bin_
        inner = bin_.get_by_name("videosink")
        # glsinkbin wraps the real sink; its widget and stats live on the gtkglsink.
        self.sink = gtk_sink or inner
        self.widget: Gtk.Widget = self.sink.props.widget
        # Frames are shown as soon as the pacer releases them; the camera's own
        # timestamps are too unreliable to schedule by.
        for el in {inner, self.sink}:
            el.set_property("sync", False)
        queue = bin_.get_by_name("q")
        self.pacer = FramePacer(queue) if PACED else None
        self.widget.set_hexpand(True)
        self.widget.set_vexpand(True)
        self.queue_drops = 0
        queue.connect("overrun", self._on_overrun)
        self._scale = bin_.get_by_name("scale")
        self._caps_prefix = caps_prefix
        self._target_width = 0

    def _on_overrun(self, _queue) -> None:
        self.queue_drops += 1

    def counters(self) -> tuple[int, int, int, int]:
        """Totals since start: (rendered, queue overflow, too late, sink dropped)."""
        stats = self.sink.get_property("stats")
        late = self.pacer.dropped if self.pacer else 0
        return stats.get_value("rendered"), self.queue_drops, late, stats.get_value("dropped")

    def set_target_width(self, width: int) -> None:
        """Scale frames to this width before drawing (no-op for the GL path)."""
        if self._scale is None:
            return
        width = max(160, width - width % 2)
        if width == self._target_width:
            return
        self._target_width = width
        log.debug("%s output: scaling frames to %dpx wide", self.kind, width)
        self._scale.set_property(
            "caps",
            Gst.Caps.from_string(f"{self._caps_prefix},width={width},pixel-aspect-ratio=1/1"),
        )

    def contains(self, obj) -> bool:
        while obj is not None:
            if obj is self.element:
                return True
            obj = obj.get_parent()
        return False


def _have(*names: str) -> bool:
    return all(Gst.ElementFactory.find(n) is not None for n in names)


def _on_wayland() -> bool:
    display = Gdk.Display.get_default()
    return display is not None and display.__gtype__.name == "GdkWaylandDisplay"


def _build_sink(kind: str) -> VideoSink | None:
    if kind == "wayland":
        if not _have("gtkwaylandsink") or not _on_wayland():
            return None
        # The compositor converts and scales on the GPU. videoconvert only kicks in
        # (on the CPU) if the compositor can't take the decoder's pixel format,
        # so it's left out in GPU-only mode.
        convert = "" if GPU_ONLY else "videoconvert n-threads=0 ! "
        desc = f"{LEAKY_QUEUE} ! {convert}gtkwaylandsink name=videosink"
        return VideoSink(kind, Gst.parse_bin_from_description(desc, True))

    if kind == "gl":
        if not _have("gtkglsink", "glsinkbin"):
            return None
        # Give glsinkbin its sink before it joins a bin; swapping it later fails.
        glsink = Gst.ElementFactory.make("gtkglsink", None)
        glbin = Gst.ElementFactory.make("glsinkbin", "videosink")
        glbin.set_property("sink", glsink)
        bin_ = Gst.parse_bin_from_description(LEAKY_QUEUE, False)
        bin_.add(glbin)
        queue = bin_.get_by_name("q")
        queue.link(glbin)
        bin_.add_pad(Gst.GhostPad.new("sink", queue.get_static_pad("sink")))
        return VideoSink(kind, bin_, gtk_sink=glsink)

    if not _have("gtksink"):
        raise RuntimeError(
            "GStreamer 'gtksink' element not found. Install it with:\n"
            "  sudo apt install gstreamer1.0-gtk3"
        )
    if kind == "cuda":
        if not _have("cudaupload", "cudaconvertscale", "cudadownload"):
            return None
        prefix = "video/x-raw(memory:CUDAMemory),format=BGRx"
        desc = (
            f"{LEAKY_QUEUE} ! cudaupload ! cudaconvertscale"
            f" ! capsfilter name=scale caps={prefix}"
            " ! cudadownload ! gtksink name=videosink"
        )
    else:
        prefix = "video/x-raw,format=BGRx"
        scaler = (
            "videoconvertscale n-threads=0"
            if _have("videoconvertscale")
            else "videoscale n-threads=0 ! videoconvert n-threads=0"
        )
        desc = f"{LEAKY_QUEUE} ! {scaler} ! capsfilter name=scale caps={prefix} ! gtksink name=videosink"
    return VideoSink(kind, Gst.parse_bin_from_description(desc, True), prefix)


class NoVideoOutput(RuntimeError):
    pass


def make_video_sink() -> VideoSink:
    kinds = SINK_ORDER if SINK_MODE not in SINK_ORDER else (SINK_MODE, "cpu")
    for kind in kinds:
        if GPU_ONLY and kind in CPU_KINDS:
            continue
        if kind in _failed_kinds and kind != "cpu":
            continue
        try:
            sink = _build_sink(kind)
        except GLib.Error as exc:
            log.warning("could not build %s video output: %s", kind, exc.message)
            sink = None
        if sink is not None:
            return sink
    raise NoVideoOutput(
        "No GPU video output works here" if GPU_ONLY else "No usable video output found"
    )


class CameraTile(Gtk.EventBox):
    """Shows one camera. Owns a playbin pipeline and restarts it on errors or stalls."""

    def __init__(self, camera: Camera):
        super().__init__()
        self.camera = camera
        self.stream_override: str | None = None
        self._wanted = False
        self._retry_id = 0
        self._backoff = RETRY_MIN_S
        self._last_frame = 0.0
        self._got_frame = False
        self._status = "Stopped"

        # Let the event box receive clicks on top of the video widget.
        self.set_above_child(True)

        # Caption bar above the video rather than drawn over it: the Wayland
        # output is a compositor layer on top of the window and would hide it.
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._box.get_style_context().add_class("camera-bg")
        self.add(self._box)
        self._sink: VideoSink | None = None
        self._sink_stale = False
        self._last_buffer: Gst.Buffer | None = None
        self._last_counters: tuple = ()
        self._resize_pending = False

        self._label = Gtk.Label(xalign=0, ellipsize=3)  # Pango.EllipsizeMode.END
        self._label.get_style_context().add_class("camera-label")
        self._box.pack_start(self._label, False, False, 0)

        self._playbin: Gst.Element | None = None
        self._new_pipeline(make_video_sink())

        self._watchdog_id = GLib.timeout_add_seconds(WATCHDOG_INTERVAL_S, self._watchdog)
        self.connect("destroy", lambda *_: self.dispose())
        self._update_label()

    # ------------------------------------------------------------------ public

    @property
    def active_stream(self) -> str:
        return self.stream_override or self.camera.stream

    @property
    def is_playing(self) -> bool:
        return self._wanted

    def set_camera(self, camera: Camera) -> None:
        """Replace the camera settings (after editing) and restart if running."""
        self.camera = camera
        self._apply_audio_flag()
        if self._wanted:
            self.restart()
        else:
            self._update_label()

    def set_stream_override(self, stream: str | None) -> None:
        if stream == self.stream_override:
            return
        old = self.active_stream
        self.stream_override = stream
        if self._wanted and old != self.active_stream:
            self.restart()
        else:
            self._update_label()

    def start(self) -> None:
        self._wanted = True
        self._cancel_retry()
        self._got_frame = False
        self._last_frame = time.monotonic()
        try:
            uri = self.camera.url(self.active_stream)
        except ValueError as exc:
            self._set_status(str(exc))
            return
        if not uri:
            self._set_status("No URL configured")
            return
        log.info("[%s] connecting to %s", self.camera.name, self.camera.display_url(self.active_stream))
        self._stop_pipeline()
        if self._sink_stale and not self._replace_stale_sink():
            return
        self._playbin.set_property("uri", uri)
        self._set_status("Connecting…")
        if self._playbin.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            if self._sink.kind != "cpu" or GPU_ONLY:
                # A synchronous failure here is almost always the GPU output
                # being unusable on this system (e.g. no GL context).
                self._fall_back()
                return
            self._schedule_reconnect("Failed to start")

    def stop(self) -> None:
        self._wanted = False
        self._cancel_retry()
        self._stop_pipeline()
        self._set_status("Stopped")

    def restart(self) -> None:
        self._backoff = RETRY_MIN_S
        self.start()

    def snapshot_png(self) -> bytes | None:
        """Return the current frame as PNG bytes, or None if nothing is playing."""
        buf = self._last_buffer
        caps = self._sink.element.get_static_pad("sink").get_current_caps() if self._sink else None
        if buf is None or caps is None:
            return None
        caps = caps.copy()
        caps.set_features(0, Gst.CapsFeatures.new_empty())
        try:
            png = GstVideo.video_convert_sample(
                Gst.Sample.new(buf, caps, None, None),
                Gst.Caps.from_string("image/png"),
                5 * Gst.SECOND,
            )
        except GLib.Error as exc:
            log.warning("[%s] snapshot failed: %s", self.camera.name, exc.message)
            return None
        buf = png.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            return bytes(info.data)
        finally:
            buf.unmap(info)

    def dispose(self) -> None:
        if self._watchdog_id:
            GLib.source_remove(self._watchdog_id)
            self._watchdog_id = 0
        self._wanted = False
        self._cancel_retry()
        if self._playbin is not None:
            self._release_playbin()
            self._last_buffer = None

    # ----------------------------------------------------------------- internal

    def _new_pipeline(self, sink: VideoSink) -> None:
        """(Re)build the playbin around a video output.

        A changed output always gets a fresh playbin: the old one caches the
        previous GL output's display and context, and a new GL output (which
        brings its own) can't use frames decoded against them.
        """
        self._release_playbin()
        playbin = Gst.ElementFactory.make("playbin", None)
        if playbin is None:
            raise RuntimeError(
                "GStreamer 'playbin' not found. Install gstreamer1.0-plugins-base."
            )
        playbin.connect("source-setup", self._on_source_setup)
        playbin.connect("deep-element-added", self._on_element_added)
        self._playbin = playbin
        self._install_sink(sink)
        self._apply_audio_flag()
        bus = playbin.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_bus_message)

    def _release_playbin(self) -> None:
        if self._playbin is None:
            return
        self._stop_pipeline()
        bus = self._playbin.get_bus()
        bus.disconnect(self._bus_handler)
        bus.remove_signal_watch()
        self._playbin = None

    def _stop_pipeline(self) -> None:
        if self._sink is not None and self._sink.pacer is not None:
            self._sink.pacer.interrupt()
        self._playbin.set_state(Gst.State.NULL)
        if self._sink is not None and self._sink.pacer is not None:
            self._sink.pacer.reset()

    def _install_sink(self, sink: VideoSink) -> None:
        """Attach a video sink to a new playbin and show its widget."""
        if self._sink is not None:
            self._box.remove(self._sink.widget)
        self._sink = sink
        self._last_buffer = None
        self._last_counters: tuple = ()
        self._playbin.set_property("video-sink", sink.element)
        sink.element.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_buffer)
        self._box.pack_start(sink.widget, True, True, 0)
        sink.widget.connect("size-allocate", self._on_video_resized)
        if sink.kind in GPU_KINDS:
            sink.widget.connect("unrealize", self._on_video_unrealized)
            sink.widget.connect("realize", self._on_video_realized)
        sink.widget.show()
        self._sink_stale = False  # removing the old widget above may have flagged it
        self._update_scale()
        log.debug("[%s] using %s video output", self.camera.name, sink.kind)

    def _on_video_unrealized(self, widget) -> None:
        # The GPU outputs stay bound to the surface they first drew on (gl: the GL
        # context of the old widget window, wayland: a subsurface of it). Once the
        # widget is re-created, e.g. after moving to another grid cell, frames still
        # flow but nothing reaches the screen, so the output must be replaced.
        if self._sink is not None and widget is self._sink.widget:
            self._sink_stale = True

    def _on_video_realized(self, _widget) -> None:
        if self._sink_stale and self._wanted:
            GLib.idle_add(self._restart_if_stale)

    def _restart_if_stale(self) -> bool:
        if self._sink_stale and self._wanted:
            log.info("[%s] video surface was re-created, restarting with a new output", self.camera.name)
            self.restart()
        return False

    def _replace_stale_sink(self) -> bool:
        """Swap in a new video output and pipeline. False if no output is usable."""
        try:
            self._new_pipeline(make_video_sink())
        except NoVideoOutput as exc:
            self._wanted = False
            self._set_status(str(exc))
            return False
        return True

    def _on_element_added(self, _bin, _sub, element) -> None:
        factory = element.get_factory()
        if factory and "Codec/Decoder" in (factory.get_metadata("klass") or ""):
            log.info("[%s] decoder: %s", self.camera.name, factory.get_name())
            # NVIDIA decoders hold frames back for reordering by default; cameras
            # don't need that, so hand each frame out as soon as it's decoded.
            if element.find_property("max-display-delay") is not None:
                element.set_property("max-display-delay", 0)
            if "Video" in factory.get_metadata("klass"):
                element.get_static_pad("src").connect("notify::caps", self._on_decoder_caps)

    def _on_decoder_caps(self, pad, _pspec) -> None:
        caps = pad.get_current_caps()
        if caps is None or not caps.get_size():
            return
        features = caps.get_features(0).to_string()
        where = "system RAM (copied to GPU for display)" if features in ("", "memory:SystemMemory") else features
        st = caps.get_structure(0)
        log.info(
            "[%s] decoded video: %dx%d %s in %s",
            self.camera.name, st.get_int("width")[1], st.get_int("height")[1],
            st.get_string("format"), where,
        )

    def _on_video_resized(self, *_):
        # Coalesce the burst of allocations during a window resize.
        if not self._resize_pending:
            self._resize_pending = True
            GLib.timeout_add(150, self._update_scale)

    def _update_scale(self) -> bool:
        """Scale frames down to the on-screen size so full 4K frames never hit the painter."""
        self._resize_pending = False
        if self._sink is None:
            return False
        widget = self._sink.widget
        width = widget.get_allocated_width() * widget.get_scale_factor()
        if width <= 1:
            width = 1920
        caps = self._sink.element.get_static_pad("sink").get_current_caps()
        if caps is not None and caps.get_size():
            ok, src_width = caps.get_structure(0).get_int("width")
            if ok:
                width = min(width, src_width)  # never upscale
        self._sink.set_target_width(width)
        return False

    def _apply_audio_flag(self) -> None:
        flags = "video+audio+soft-volume" if self.camera.audio else "video"
        Gst.util_set_object_arg(self._playbin, "flags", flags)

    def _on_source_setup(self, _playbin, source) -> None:
        factory = source.get_factory()
        name = factory.get_name() if factory else ""
        if name == "rtspsrc":
            # Reolink RTSP over UDP drops lots of packets; TCP is far more reliable.
            source.set_property("latency", RTSP_LATENCY_MS)
            Gst.util_set_object_arg(source, "protocols", "tcp")
        elif name in ("souphttpsrc",):
            source.set_property("is-live", True)

    def _on_buffer(self, _pad, info):
        # Runs on a streaming thread: only touch simple attributes here.
        self._last_frame = time.monotonic()
        self._last_buffer = info.get_buffer()
        if not self._got_frame:
            self._got_frame = True
            GLib.idle_add(self._on_first_frame)
        return Gst.PadProbeReturn.OK

    def _on_first_frame(self) -> bool:
        self._update_scale()  # source size is known now
        if self._wanted:
            self._backoff = RETRY_MIN_S
            self._set_status("Live")
        return False

    def _on_bus_message(self, _bus, msg: Gst.Message) -> None:
        if msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.warning("[%s] error: %s (%s)", self.camera.name, err.message, debug)
            if (self._sink.kind != "cpu" or GPU_ONLY) and (
                self._sink.contains(msg.src)
                or (not self._got_frame and "not-negotiated" in (debug or ""))
            ):
                self._fall_back()
                return
            reason = err.message
            if GPU_ONLY and err.matches(Gst.CoreError.quark(), Gst.CoreError.MISSING_PLUGIN):
                reason = "No GPU decoder for this stream (GPU-only mode)"
            self._schedule_reconnect(reason)
        elif msg.type == Gst.MessageType.EOS:
            log.info("[%s] end of stream", self.camera.name)
            self._schedule_reconnect("Stream ended")

    def _fall_back(self) -> None:
        failed = self._sink.kind
        _failed_kinds.add(failed)
        self._stop_pipeline()
        try:
            sink = make_video_sink()
        except NoVideoOutput as exc:
            log.error("%s video output failed and no other is allowed: %s", failed, exc)
            self._wanted = False
            self._cancel_retry()
            hint = " (turn off “GPU only” in the menu)" if GPU_ONLY else ""
            self._set_status(f"{exc}{hint}")
            return
        self._new_pipeline(sink)
        log.warning("%s video output failed; switching to %s", failed, self._sink.kind)
        if self._wanted:
            self.start()

    def _watchdog(self) -> bool:
        if self._wanted and self._got_frame and log.isEnabledFor(logging.DEBUG):
            now = self._sink.counters()
            prev = self._last_counters
            if len(prev) == len(now) and now[0] >= prev[0]:
                shown, overflow, late, sink = (a - b for a, b in zip(now, prev))
                log.debug(
                    "[%s] %.1f fps shown, dropped: %d overflow / %d late / %d display "
                    "(%s output), smoothing buffer %s, app CPU %.0f%%",
                    self.camera.name,
                    shown / WATCHDOG_INTERVAL_S,
                    overflow, late, sink,
                    self._sink.kind,
                    f"{self._sink.pacer.cushion:.2f}s" if self._sink.pacer else "off",
                    process_cpu_percent(),
                )
            self._last_counters = now
        if (
            self._wanted
            and not self._retry_id
            and time.monotonic() - self._last_frame > STALL_TIMEOUT_S
        ):
            log.warning("[%s] no frames for %ss, reconnecting", self.camera.name, STALL_TIMEOUT_S)
            self._schedule_reconnect("No video")
        return True

    def _schedule_reconnect(self, reason: str) -> None:
        if self._playbin is None:
            return
        self._stop_pipeline()
        if not self._wanted or self._retry_id:
            return
        delay = self._backoff
        self._backoff = min(self._backoff * 2, RETRY_MAX_S)
        self._set_status(f"{reason} — retrying in {delay}s")
        self._retry_id = GLib.timeout_add_seconds(delay, self._on_retry)

    def _on_retry(self) -> bool:
        self._retry_id = 0
        if self._wanted:
            self.start()
        return False

    def _cancel_retry(self) -> None:
        if self._retry_id:
            GLib.source_remove(self._retry_id)
            self._retry_id = 0

    def _set_status(self, status: str) -> None:
        self._status = status
        self._update_label()

    def _update_label(self) -> None:
        stream = self.active_stream if self.camera.protocol != "custom" else "custom"
        text = f"{self.camera.name}  ·  {stream}  ·  {self._status}"
        self._label.set_text(text)
        try:
            self.set_tooltip_text(self.camera.display_url(self.active_stream))
        except ValueError:
            self.set_tooltip_text(None)
