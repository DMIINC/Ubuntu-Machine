"""A GTK widget that plays one camera stream through GStreamer, with auto-reconnect."""

from __future__ import annotations

import logging
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gst, Gtk  # noqa: E402

from .config import Camera  # noqa: E402

log = logging.getLogger(__name__)

RTSP_LATENCY_MS = 200
RETRY_MIN_S = 2
RETRY_MAX_S = 30
STALL_TIMEOUT_S = 20
WATCHDOG_INTERVAL_S = 5


def make_video_sink() -> Gst.Element:
    sink = Gst.ElementFactory.make("gtksink", None)
    if sink is None:
        raise RuntimeError(
            "GStreamer 'gtksink' element not found. Install it with:\n"
            "  sudo apt install gstreamer1.0-gtk3"
        )
    return sink


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

        overlay = Gtk.Overlay()
        overlay.get_style_context().add_class("camera-bg")
        self.add(overlay)

        self._sink = make_video_sink()
        video = self._sink.props.widget
        video.set_hexpand(True)
        video.set_vexpand(True)
        overlay.add(video)

        self._label = Gtk.Label(xalign=0)
        self._label.set_halign(Gtk.Align.START)
        self._label.set_valign(Gtk.Align.START)
        self._label.get_style_context().add_class("camera-label")
        overlay.add_overlay(self._label)

        self._playbin = Gst.ElementFactory.make("playbin", None)
        if self._playbin is None:
            raise RuntimeError(
                "GStreamer 'playbin' not found. Install gstreamer1.0-plugins-base."
            )
        self._playbin.set_property("video-sink", self._sink)
        self._playbin.connect("source-setup", self._on_source_setup)
        self._apply_audio_flag()

        bus = self._playbin.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_bus_message)

        self._sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._on_buffer
        )
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
        sample = self._playbin.emit("convert-sample", Gst.Caps.from_string("image/png"))
        if sample is None:
            return None
        buf = sample.get_buffer()
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
            bus = self._playbin.get_bus()
            bus.disconnect(self._bus_handler)
            bus.remove_signal_watch()
            self._playbin = None

    # ----------------------------------------------------------------- internal

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

    def _on_buffer(self, _pad, _info):
        # Runs on a streaming thread: only touch simple attributes here.
        self._last_frame = time.monotonic()
        if not self._got_frame:
            self._got_frame = True
            GLib.idle_add(self._on_first_frame)
        return Gst.PadProbeReturn.OK

    def _on_first_frame(self) -> bool:
        if self._wanted:
            self._backoff = RETRY_MIN_S
            self._set_status("Live")
        return False

    def _on_bus_message(self, _bus, msg: Gst.Message) -> None:
        if msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.warning("[%s] error: %s (%s)", self.camera.name, err.message, debug)
            self._schedule_reconnect(err.message)
        elif msg.type == Gst.MessageType.EOS:
            log.info("[%s] end of stream", self.camera.name)
            self._schedule_reconnect("Stream ended")

    def _watchdog(self) -> bool:
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
