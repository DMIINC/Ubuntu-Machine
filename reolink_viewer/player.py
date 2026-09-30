"""A GTK widget that plays one camera stream through GStreamer, with auto-reconnect."""

from __future__ import annotations

import logging
import os
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo, Gtk  # noqa: E402

from .config import Camera  # noqa: E402

log = logging.getLogger(__name__)

RTSP_LATENCY_MS = 300
RETRY_MIN_S = 2
RETRY_MAX_S = 30
STALL_TIMEOUT_S = 20
WATCHDOG_INTERVAL_S = 5


# Video output paths, best first. Each one that fails is skipped from then on:
#   gl   - gtkglsink: GPU scales and draws (Intel/AMD, X11)
#   cuda - NVIDIA GPU converts + scales to the window size, gtksink draws the result
#   cpu  - multithreaded convert + scale to the window size, gtksink draws
# REOLINK_VIEWER_SINK=gl|cuda|cpu forces a path ("sw" is an alias for cpu).
SINK_ORDER = ("gl", "cuda", "cpu")
SINK_MODE = os.environ.get("REOLINK_VIEWER_SINK", "auto").lower().replace("sw", "cpu")
_failed_kinds: set[str] = set()

# Hold at most two decoded frames. If drawing falls behind, older frames are
# thrown away instead of piling up, so the picture never lags behind live.
LEAKY_QUEUE = "queue name=q max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream"


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
        # Live view: show each frame as soon as it's decoded instead of waiting
        # on (often jittery) camera timestamps.
        inner.set_property("sync", False)
        self.widget.set_hexpand(True)
        self.widget.set_vexpand(True)
        self.queue_drops = 0
        bin_.get_by_name("q").connect("overrun", self._on_overrun)
        self._scale = bin_.get_by_name("scale")
        self._caps_prefix = caps_prefix
        self._target_width = 0

    def _on_overrun(self, _queue) -> None:
        self.queue_drops += 1

    def counters(self) -> tuple[int, int]:
        """(rendered, dropped) frame totals since the sink started."""
        stats = self.sink.get_property("stats")
        return stats.get_value("rendered"), stats.get_value("dropped") + self.queue_drops

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


def _build_sink(kind: str) -> VideoSink | None:
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


def make_video_sink() -> VideoSink:
    kinds = SINK_ORDER if SINK_MODE not in SINK_ORDER else (SINK_MODE, "cpu")
    for kind in kinds:
        if kind in _failed_kinds and kind != "cpu":
            continue
        try:
            sink = _build_sink(kind)
        except GLib.Error as exc:
            log.warning("could not build %s video output: %s", kind, exc.message)
            sink = None
        if sink is not None:
            return sink
    raise RuntimeError("No usable video output found")


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

        self._overlay = Gtk.Overlay()
        self._overlay.get_style_context().add_class("camera-bg")
        self.add(self._overlay)
        self._sink: VideoSink | None = None
        self._last_buffer: Gst.Buffer | None = None
        self._last_counters = (0, 0)
        self._resize_pending = False

        self._label = Gtk.Label(xalign=0)
        self._label.set_halign(Gtk.Align.START)
        self._label.set_valign(Gtk.Align.START)
        self._label.get_style_context().add_class("camera-label")
        self._overlay.add_overlay(self._label)

        self._playbin = Gst.ElementFactory.make("playbin", None)
        if self._playbin is None:
            raise RuntimeError(
                "GStreamer 'playbin' not found. Install gstreamer1.0-plugins-base."
            )
        self._playbin.connect("source-setup", self._on_source_setup)
        self._playbin.connect("deep-element-added", self._on_element_added)
        self._install_sink(make_video_sink())
        self._apply_audio_flag()

        bus = self._playbin.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_bus_message)

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
        self._playbin.set_state(Gst.State.NULL)
        self._playbin.set_property("uri", uri)
        self._set_status("Connecting…")
        if self._playbin.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            if self._sink.kind != "cpu":
                # A synchronous failure here is almost always the GPU output
                # being unusable on this system (e.g. no GL context).
                self._fall_back()
                return
            self._schedule_reconnect("Failed to start")

    def stop(self) -> None:
        self._wanted = False
        self._cancel_retry()
        self._playbin.set_state(Gst.State.NULL)
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
            self._playbin.set_state(Gst.State.NULL)
            self._last_buffer = None
            bus = self._playbin.get_bus()
            bus.disconnect(self._bus_handler)
            bus.remove_signal_watch()
            self._playbin = None

    # ----------------------------------------------------------------- internal

    def _install_sink(self, sink: VideoSink) -> None:
        """Attach a video sink to playbin and show its widget. Playbin must be in NULL."""
        if self._sink is not None:
            self._overlay.remove(self._sink.widget)
        self._sink = sink
        self._last_buffer = None
        self._last_counters = (0, 0)
        self._playbin.set_property("video-sink", sink.element)
        sink.element.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_buffer)
        self._overlay.add(sink.widget)
        sink.widget.connect("size-allocate", self._on_video_resized)
        sink.widget.show()
        self._update_scale()
        log.debug("[%s] using %s video output", self.camera.name, sink.kind)

    def _on_element_added(self, _bin, _sub, element) -> None:
        factory = element.get_factory()
        if factory and "Codec/Decoder" in (factory.get_metadata("klass") or ""):
            log.info("[%s] decoder: %s", self.camera.name, factory.get_name())
            # NVIDIA decoders hold frames back for reordering by default; cameras
            # don't need that, so hand each frame out as soon as it's decoded.
            if element.find_property("max-display-delay") is not None:
                element.set_property("max-display-delay", 0)

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
            if self._sink.kind != "cpu" and (
                self._sink.contains(msg.src)
                or (not self._got_frame and "not-negotiated" in (debug or ""))
            ):
                self._fall_back()
                return
            self._schedule_reconnect(err.message)
        elif msg.type == Gst.MessageType.EOS:
            log.info("[%s] end of stream", self.camera.name)
            self._schedule_reconnect("Stream ended")

    def _fall_back(self) -> None:
        failed = self._sink.kind
        _failed_kinds.add(failed)
        self._playbin.set_state(Gst.State.NULL)
        self._install_sink(make_video_sink())
        log.warning("%s video output failed; switching to %s", failed, self._sink.kind)
        if self._wanted:
            self.start()

    def _watchdog(self) -> bool:
        if self._wanted and self._got_frame and log.isEnabledFor(logging.DEBUG):
            rendered, dropped = self._sink.counters()
            prev_r, prev_d = self._last_counters
            if rendered >= prev_r:
                log.debug(
                    "[%s] %.1f fps shown, %d dropped (%s output)",
                    self.camera.name,
                    (rendered - prev_r) / WATCHDOG_INTERVAL_S,
                    dropped - prev_d,
                    self._sink.kind,
                )
            self._last_counters = (rendered, dropped)
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
        self._playbin.set_state(Gst.State.NULL)
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
