"""A GTK widget that plays one recording: the video, and below it play, seek and speed controls.

Separate from the live CameraTile on purpose. Live video ignores the camera's
time stamps: a leaky queue and FramePacer show frames as they arrive. A
recording arrives as fast as it can be read, so here the sink shows each frame
at its time stamp (sync=TRUE), which is also what makes pause, seek and speed
work. There is no queue or pacer in between.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gst, Gtk  # noqa: E402

from . import player  # noqa: E402
from .config import redact_url  # noqa: E402

log = logging.getLogger(__name__)

SPEEDS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
OPEN_TIMEOUT_S = 15  # some failures never post an error: no picture by then means give up
SEEK_TIMEOUT_S = 10  # a jump on a source that can't seek may never be answered
STALL_TIMEOUT_S = 20  # playing, but no new frame for this long
POSITION_INTERVAL_MS = 200
# playbin's buffer for http(s). Small, so the pause after a jump stays short
# (the default 2 MB cost 2.7 s of pauses at 1 MB/s).
NET_BUFFER_BYTES = 512 * 1024
HTTP_TIMEOUT_S = 15  # souphttpsrc then asks again from where it stopped, by itself

# Error kinds that mean "this can't be played from the camera: download it and play the file".
NEEDS_DOWNLOAD = ("no-range", "no-moov")

# Every video sink ever made. A gtkglsink must never be freed: on Wayland that
# terminates the EGL display GTK shares, and all GL drawing fails from then on.
_sinks: list[tuple[Gst.Element, Gst.Element]] = []


def format_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    hours, rest = divmod(int(seconds + 0.5), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _output_kinds() -> list[str]:
    """Video outputs worth trying, best first: the live player's order, minus
    what GPU-only mode forbids and what has failed on this system."""
    order = player.SINK_ORDER if player.SINK_MODE not in player.SINK_ORDER else (player.SINK_MODE, "cpu")
    kinds: list[str] = []
    for kind in order:
        if kind not in ("gl", "wayland"):
            kind = "cpu"  # one CPU-drawn output will do: recordings aren't scaled on a GPU first
            if player.GPU_ONLY:
                continue
        elif kind in player._failed_kinds:
            continue
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def _build_output(kind: str) -> tuple[Gst.Element, Gst.Element] | None:
    """(what playbin gets as its video sink, the GTK sink inside it), or None if
    this system can't do that kind. The same outputs as the live player's, without
    its queue: gl draws on the GPU, wayland hands frames to the compositor."""
    if kind == "gl":
        if not player._have("gtkglsink", "glsinkbin"):
            return None
        # Give glsinkbin its sink before it joins a bin; swapping it later fails.
        gtk_sink = Gst.ElementFactory.make("gtkglsink", None)
        element = Gst.ElementFactory.make("glsinkbin", None)
        element.set_property("sink", gtk_sink)
        element.set_property("sync", True)
    else:
        if kind == "wayland":
            if not player._have("gtkwaylandsink") or not player._on_wayland():
                return None
            # videoconvert only steps in (on the CPU) for a pixel format the
            # compositor can't take, so GPU-only mode leaves it out.
            description = "gtkwaylandsink name=videosink"
            if not player.GPU_ONLY:
                description = f"videoconvert n-threads=0 ! {description}"
        elif player._have("gtksink"):
            description = "videoconvert n-threads=0 ! gtksink name=videosink"
        else:
            return None
        element = Gst.parse_bin_from_description(description, True)
        gtk_sink = element.get_by_name("videosink")
    gtk_sink.set_property("sync", True)
    _sinks.append((element, gtk_sink))
    return element, gtk_sink


def classify_error(err: GLib.Error, debug: str | None, streamed: bool,
                   from_source: bool) -> tuple[str, str]:
    """(kind, text for the user) for a GStreamer error. streamed: the clip comes
    over http(s); from_source: the element fetching it reported the error.

    Kinds: "no-range" and "no-moov" (see NEEDS_DOWNLOAD), "not-found", "not-video",
    "no-decoder", "network", "other".
    """
    debug = debug or ""
    if err.matches(Gst.ResourceError.quark(), Gst.ResourceError.SEEK):
        return "no-range", "The camera can't jump around in this recording while sending it"
    if "no 'moov' atom" in debug or "no known streams found" in debug:
        return "no-moov", "This recording can only be played once it has been downloaded"
    if err.matches(Gst.ResourceError.quark(), Gst.ResourceError.NOT_FOUND):
        return "not-found", "The recording was not found"
    if err.matches(Gst.StreamError.quark(), Gst.StreamError.TYPE_NOT_FOUND):
        if streamed:
            return "not-video", "The camera didn't send a video (its login may have run out)"
        return "not-video", "This is not a video"
    if err.matches(Gst.CoreError.quark(), Gst.CoreError.MISSING_PLUGIN) or err.matches(
            Gst.StreamError.quark(), Gst.StreamError.CODEC_NOT_FOUND):
        if player.GPU_ONLY:
            return "no-decoder", "No GPU decoder for this recording (GPU-only mode)"
        return "no-decoder", "No decoder for this recording"
    if streamed and from_source:
        if err.matches(Gst.StreamError.quark(), Gst.StreamError.FAILED):
            return "network", "The connection to the camera failed"  # GStreamer gives no reason
        return "network", f"The connection to the camera failed ({err.message})"
    return "other", err.message


def _masked(text: str | None) -> str:
    """GStreamer's error details quote the URL, token and all: mask it for the log."""
    return " ".join(redact_url(word) if "://" in word else word for word in (text or "").split())


class ClipPlayer(Gtk.Box):
    """Plays one recording at a time. States: idle, opening, playing, paused, ended, error.

    Put it in a window before the first open(), and never move it to another
    container: the GPU outputs stay bound to the surface they first drew on.
    Make one and keep it (hide its window instead of destroying it), because
    each player's video sink stays in memory for good.

    The owner may set on_state(state), called when the state changes,
    on_ended(), called when a clip has played to its end, and
    on_error(kind, text) with a kind from classify_error().
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_state: Callable[[str], None] | None = None
        self.on_ended: Callable[[], None] | None = None
        self.on_error: Callable[[str, str], None] | None = None
        self._state = "idle"
        self._uri = ""
        self._hint: float | None = None  # the length the camera's recording list gave
        self._audio = False
        self._duration: float | None = None
        self._seekable = False
        self._speed = 1.0
        self._want_play = True
        self._prerolled = False  # the first picture is there: position and seeking work
        self._buffering = False
        self._stepped = False
        self._seek_busy = False
        self._seek_next: tuple[float, bool] | None = None  # asked for while a jump is under way
        self._dragging = False
        self._frames = 0  # buffers that reached the sink (counted on a streaming thread)
        self._progress = (0, 0.0)  # (frames seen, when that last changed), for the stall check
        self._open_guard = 0
        self._seek_guard = 0
        self._timer = 0
        self._closed = False
        self._playbin: Gst.Element | None = None
        self._bus_handlers: list[int] = []
        self._kind = ""
        self._element: Gst.Element | None = None
        self._gtk_sink: Gst.Element | None = None
        self._video: Gtk.Widget | None = None

        self._stage = Gtk.Box()  # holds the video widget (and, hidden, any that failed)
        self.pack_start(self._stage, True, True, 0)

        controls = Gtk.Box(spacing=6)
        controls.set_border_width(4)
        self._play_button = Gtk.Button.new_from_icon_name(
            "media-playback-start-symbolic", Gtk.IconSize.BUTTON)
        self._play_button.connect("clicked", lambda _button: self.toggle())
        self._step_button = Gtk.Button.new_from_icon_name(
            "media-skip-forward-symbolic", Gtk.IconSize.BUTTON)
        self._step_button.set_tooltip_text("Next frame")
        self._step_button.connect("clicked", lambda _button: self.step())
        # Built from an adjustment: new_with_range() would round dragging to whole seconds.
        self._scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                                adjustment=Gtk.Adjustment(0, 0, 1, 1, 10, 0))
        self._scale.set_draw_value(False)
        self._scale.set_hexpand(True)
        self._scale.connect("change-value", self._on_scale_moved)
        self._scale.connect("button-press-event", self._on_scale_pressed)
        self._scale.connect("button-release-event", self._on_scale_released)
        self._time_label = Gtk.Label(xalign=1)
        self._time_label.set_width_chars(13)
        self._speed_box = Gtk.ComboBoxText()
        for speed in SPEEDS:
            self._speed_box.append_text(f"{speed:g}x")
        self._speed_box.set_active(SPEEDS.index(1.0))
        self._speed_box.set_tooltip_text("Playback speed")
        self._speed_box.connect("changed", lambda box: self.set_speed(SPEEDS[box.get_active()]))
        controls.pack_start(self._play_button, False, False, 0)
        controls.pack_start(self._step_button, False, False, 0)
        controls.pack_start(self._scale, True, True, 0)
        controls.pack_start(self._time_label, False, False, 0)
        controls.pack_start(self._speed_box, False, False, 0)
        self.pack_start(controls, False, False, 0)
        self._status_label = Gtk.Label(xalign=0, ellipsize=3)  # Pango.EllipsizeMode.END
        self._status_label.set_margin_start(6)
        self._status_label.set_margin_bottom(4)
        self.pack_start(self._status_label, False, False, 0)

        self._use_next_output()
        self._show_idle()
        self.connect("destroy", lambda *_: self.shutdown())
        self.show_all()

    # ------------------------------------------------------------------ public

    @property
    def state(self) -> str:
        return self._state

    @property
    def status(self) -> str:
        """The line under the controls: what is going on, or what went wrong."""
        return self._status_label.get_text()

    @property
    def position(self) -> float | None:
        """Seconds into the recording, or None before its first picture."""
        if self._playbin is None or not self._prerolled:
            return None
        ok, value = self._playbin.query_position(Gst.Format.TIME)
        return value / Gst.SECOND if ok and value >= 0 else None

    @property
    def duration(self) -> float | None:
        """Length in seconds: the stream's own once it is known, else the hint given to open()."""
        return self._duration

    @property
    def seekable(self) -> bool:
        return self._seekable

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def output(self) -> str:
        """The video output in use: "gl", "wayland", "cpu", or "" if none works."""
        return self._kind

    def open(self, uri: str, duration_hint_seconds: float | None = None, audio: bool = False) -> None:
        """Play `uri` (file:// or http(s)://) from its start, in place of whatever was open.

        The hint is the length to show when the stream doesn't know its own.
        Problems are reported through on_error.
        """
        if self._closed:
            return
        self._uri, self._hint, self._audio = uri, duration_hint_seconds, audio
        self._want_play = True
        self._start()

    def play(self) -> None:
        if self._state == "ended":
            self._want_play = True
            if self._seekable:
                self.seek(0.0)
            else:
                self._start()  # a stream that can't go back is fetched again
        elif self._state in ("opening", "playing", "paused"):
            self._want_play = True
            if self._prerolled and not self._buffering:
                if self._stepped and self._audio:
                    self.seek(self.position or 0.0)  # frame steps moved the picture, not the sound
                self._stepped = False
                self._playbin.set_state(Gst.State.PLAYING)
                self._set_state("playing")

    def pause(self) -> None:
        if self._state in ("opening", "playing", "paused"):
            self._want_play = False
            if self._prerolled:
                self._playbin.set_state(Gst.State.PAUSED)
                self._set_state("paused")

    def toggle(self) -> None:
        if self._state == "playing":
            self.pause()
        else:
            self.play()

    def seek(self, seconds: float, accurate: bool = True) -> None:
        """Jump to `seconds`. accurate=False settles for the key frame before it,
        which is quicker (used while dragging). One jump runs at a time; of those
        asked for meanwhile only the newest is carried out."""
        if not self._prerolled or not self._seekable:
            return  # a seek sent to a stream that can't seek is fatal, or hangs
        if self._duration:
            seconds = min(seconds, max(0.0, self._duration - 0.05))
        self._seek_next = (max(0.0, seconds), accurate)
        if not self._seek_busy:
            self._send_seek()

    def set_speed(self, speed: float) -> None:
        """Play `speed` times as fast (one of SPEEDS). Needs a recording that can seek."""
        if speed == self._speed or not (self._prerolled and self._seekable):
            return
        self._speed = speed
        self._speed_box.set_active(SPEEDS.index(speed))
        self._playbin.set_property("mute", speed != 1.0)  # sound at another speed is noise
        position = self.position
        if position is not None and self._state in ("playing", "paused"):
            self.seek(position)  # the speed travels with a seek

    def step(self) -> None:
        """Show the next frame and stay paused."""
        if self._state not in ("playing", "paused") or self._seek_busy:
            return
        self.pause()
        self._stepped = True
        self._gtk_sink.send_event(Gst.Event.new_step(Gst.Format.BUFFERS, 1, 1.0, True, False))

    def stop(self) -> None:
        """Close the recording; this also drops the connection to the camera."""
        self._halt()
        self._uri = ""
        self._show_idle()
        self._set_state("idle")

    def shutdown(self) -> None:
        """Stop for good (window closed, app exiting). Safe to call more than once.
        The video sink is kept alive on purpose, see _sinks."""
        if self._closed:
            return
        self._closed = True
        self._halt()
        self._release_playbin()
        self._state = "idle"  # no callbacks: the owner is closing down too

    # ---------------------------------------------------------------- pipeline

    def _use_next_output(self) -> bool:
        """Build the best video output still allowed, with a playbin of its own
        (a playbin caches the GL display and context of the output it started with)."""
        for kind in _output_kinds():
            try:
                built = _build_output(kind)
            except GLib.Error as exc:
                log.warning("could not build %s video output: %s", kind, exc.message)
                built = None
            if built is not None:
                break
        else:
            self._kind = ""
            return False
        self._kind = kind
        self._element, self._gtk_sink = built
        playbin = Gst.ElementFactory.make("playbin", None)
        playbin.set_property("video-sink", self._element)
        playbin.connect("source-setup", self._on_source_setup)
        playbin.connect("deep-element-added", self._on_element_added)
        self._element.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_buffer)
        bus = playbin.get_bus()
        bus.add_signal_watch()
        self._bus_handlers = [bus.connect("message", self._on_message)]
        # Decode with the CUDA context the live tiles share: one more costs ~120 MB of GPU memory.
        bus.enable_sync_message_emission()
        self._bus_handlers += [bus.connect(f"sync-message::{name}", player._share_cuda_context)
                               for name in ("need-context", "have-context")]
        self._playbin = playbin
        # Packed here once and never moved, see the class notes.
        self._video = self._gtk_sink.props.widget
        self._video.set_hexpand(True)
        self._video.set_vexpand(True)
        self._stage.pack_start(self._video, True, True, 0)
        self._video.show()
        log.debug("recordings use the %s video output", kind)
        return True

    def _release_playbin(self) -> None:
        if self._playbin is None:
            return
        self._playbin.set_state(Gst.State.NULL)
        bus = self._playbin.get_bus()
        for handler in self._bus_handlers:
            bus.disconnect(handler)
        bus.remove_signal_watch()
        bus.disable_sync_message_emission()
        self._playbin = None

    def _fall_back(self) -> None:
        """The video output doesn't work on this system: remember that (for the
        live tiles too) and start the clip again on the next one allowed."""
        failed = self._kind
        player._failed_kinds.add(failed)
        self._halt()
        self._release_playbin()
        self._video.set_no_show_all(True)
        self._video.hide()
        if self._use_next_output():
            log.warning("%s video output failed; switching to %s", failed, self._kind)
            self._start()
        else:
            log.error("%s video output failed and no other is allowed", failed)
            self._fail("other", self._no_output_text())

    def _no_output_text(self) -> str:
        if player.GPU_ONLY:
            return "No GPU video output works here (turn off “GPU only” in the menu)"
        return "No usable video output found"

    def _start(self) -> None:
        self._halt()
        self._duration = self._hint
        self._seekable = False
        self._stepped = False
        self._frames = 0
        self._progress = (0, time.monotonic())
        self._speed = 1.0
        self._speed_box.set_active(SPEEDS.index(1.0))
        self._show_idle()
        if self._playbin is None:
            self._fail("other", self._no_output_text())
            return
        if not self.get_toplevel().is_toplevel():
            # A GTK sink started outside a window opens a window of its own and
            # moves its widget's whole parent chain into it.
            raise RuntimeError("put the ClipPlayer in a window before open()")
        log.info("opening %s (%s output)", redact_url(self._uri), self._kind)
        self._playbin.set_property("uri", self._uri)
        Gst.util_set_object_arg(self._playbin, "flags",
                                "video+audio+soft-volume" if self._audio else "video")
        self._playbin.set_property("mute", False)
        network = self._uri.startswith(("http://", "https://"))
        self._playbin.set_property("buffer-size", NET_BUFFER_BYTES if network else -1)
        self._set_state("opening", "Opening…")
        self._open_guard = GLib.timeout_add_seconds(OPEN_TIMEOUT_S, self._on_open_timeout)
        self._timer = GLib.timeout_add(POSITION_INTERVAL_MS, self._on_tick)
        # PAUSED first: at the first picture the length and whether it can seek are known.
        if self._playbin.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            message = self._playbin.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if message is not None:
                self._on_error_message(message)
            else:
                self._fail("other", "The recording could not be started")

    def _halt(self) -> None:
        """Stop the pipeline and everything that waits on it."""
        for name in ("_open_guard", "_seek_guard", "_timer"):
            if getattr(self, name):
                GLib.source_remove(getattr(self, name))
                setattr(self, name, 0)
        self._prerolled = False
        self._seek_busy = False
        self._seek_next = None
        self._buffering = False
        if self._playbin is not None:
            self._playbin.set_state(Gst.State.NULL)
            # Messages of the clip just stopped must not be taken for the next one's.
            bus = self._playbin.get_bus()
            bus.set_flushing(True)
            bus.set_flushing(False)

    def _set_state(self, state: str, status: str = "") -> None:
        changed = state != self._state
        self._state = state
        self._status_label.set_text(status)
        self._status_label.set_tooltip_text(status or None)
        playing = state == "playing"
        self._play_button.get_image().set_from_icon_name(
            "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic",
            Gtk.IconSize.BUTTON)
        self._play_button.set_tooltip_text("Pause" if playing else "Play")
        self._play_button.set_sensitive(state not in ("idle", "error"))
        self._step_button.set_sensitive(state in ("playing", "paused"))
        if changed and self.on_state is not None:
            self.on_state(state)

    def _show_idle(self) -> None:
        self._scale.set_sensitive(False)
        self._scale.set_value(0)
        self._speed_box.set_sensitive(False)
        self._time_label.set_text(f"--:-- / {format_time(self._hint if self._uri else None)}")

    def _fail(self, kind: str, text: str) -> None:
        log.warning("recording failed (%s): %s", kind, text)
        self._halt()
        self._scale.set_sensitive(False)
        self._speed_box.set_sensitive(False)
        self._set_state("error", text)
        if self.on_error is not None:
            self.on_error(kind, text)

    def _on_source_setup(self, _playbin, source) -> None:
        factory = source.get_factory()
        if factory is not None and factory.get_name() == "souphttpsrc":
            # A recording is a file: not is-live, unlike the live player's HTTP-FLV.
            source.set_property("timeout", HTTP_TIMEOUT_S)
            source.set_property("ssl-strict", False)  # cameras present self-signed certificates

    def _on_element_added(self, _bin, _sub, element) -> None:
        factory = element.get_factory()
        if factory is not None and "Codec/Decoder/Video" in (factory.get_metadata("klass") or ""):
            log.info("recording decoder: %s", factory.get_name())

    def _on_buffer(self, _pad, _info):
        self._frames += 1  # streaming thread: nothing else here
        return Gst.PadProbeReturn.OK

    def _in_output(self, obj) -> bool:
        while obj is not None:
            if obj is self._element:
                return True
            obj = obj.get_parent()
        return False

    # -------------------------------------------------------------------- bus

    def _on_message(self, _bus, msg: Gst.Message) -> None:
        kind = msg.type
        if kind == Gst.MessageType.ERROR:
            self._on_error_message(msg)
        elif kind == Gst.MessageType.ASYNC_DONE:
            if not self._prerolled:
                self._on_first_picture()
            elif self._seek_busy:
                self._seek_busy = False
                if self._seek_guard:
                    GLib.source_remove(self._seek_guard)
                    self._seek_guard = 0
                if self._seek_next is not None:
                    self._send_seek()
            self._show_position()
        elif kind == Gst.MessageType.EOS:
            self._playbin.set_state(Gst.State.PAUSED)  # keeps the last frame on screen
            self._set_state("ended", "End of recording")
            self._show_position(at_end=True)
            if self.on_ended is not None:
                self.on_ended()
        elif kind == Gst.MessageType.BUFFERING:
            self._on_buffering(msg.parse_buffering())
        elif kind == Gst.MessageType.DURATION_CHANGED:
            if self._prerolled:
                self._read_stream_info()
        elif kind == Gst.MessageType.CLOCK_LOST:
            if self._state == "playing":
                self._playbin.set_state(Gst.State.PAUSED)
                self._playbin.set_state(Gst.State.PLAYING)

    def _on_error_message(self, msg: Gst.Message) -> None:
        err, debug = msg.parse_error()
        log.warning("recording error from %s: %s (%s)", msg.src.get_name(), err.message, _masked(debug))
        if self._kind != "cpu" and (
            self._in_output(msg.src) or (not self._frames and "not-negotiated" in (debug or ""))
        ):
            # As for the live tiles: the GPU output can't be used on this system.
            self._fall_back()
            return
        streamed = self._uri.startswith(("http://", "https://"))
        from_source = msg.src == self._playbin.get_property("source")
        self._fail(*classify_error(err, debug, streamed, from_source))

    def _on_first_picture(self) -> None:
        self._prerolled = True
        if self._open_guard:
            GLib.source_remove(self._open_guard)
            self._open_guard = 0
        self._read_stream_info()
        if self._want_play and not self._buffering:
            self._playbin.set_state(Gst.State.PLAYING)
            self._set_state("playing")
        else:
            self._set_state("paused", "Buffering…" if self._buffering else "")

    def _read_stream_info(self) -> None:
        ok, duration = self._playbin.query_duration(Gst.Format.TIME)
        if ok and duration > 0:
            self._duration = duration / Gst.SECOND
        query = Gst.Query.new_seeking(Gst.Format.TIME)
        self._seekable = bool(self._playbin.query(query) and query.parse_seeking()[1])
        if self._duration:
            self._scale.set_range(0, self._duration)
        self._scale.set_sensitive(bool(self._duration) and self._seekable)
        self._speed_box.set_sensitive(self._seekable)

    def _on_buffering(self, percent: int) -> None:
        if percent < 100 and not self._buffering:
            self._buffering = True
            if self._state == "playing":
                self._playbin.set_state(Gst.State.PAUSED)
                self._status_label.set_text("Buffering…")
        elif percent >= 100 and self._buffering:
            self._buffering = False
            if self._prerolled and self._want_play and self._state in ("playing", "paused"):
                self._playbin.set_state(Gst.State.PLAYING)
                self._set_state("playing")

    # ------------------------------------------------------- seeking and time

    def _send_seek(self) -> None:
        seconds, accurate = self._seek_next
        self._seek_next = None
        flags = Gst.SeekFlags.FLUSH | (Gst.SeekFlags.ACCURATE if accurate else Gst.SeekFlags.KEY_UNIT)
        if self._speed > 2.0:
            flags |= Gst.SeekFlags.TRICKMODE  # lets decoders skip frames
        if not self._playbin.seek(self._speed, Gst.Format.TIME, flags, Gst.SeekType.SET,
                                  int(seconds * Gst.SECOND), Gst.SeekType.NONE, -1):
            self._status_label.set_text("Can't jump in this recording")
            return
        self._seek_busy = True
        if self._seek_guard:
            GLib.source_remove(self._seek_guard)
        self._seek_guard = GLib.timeout_add_seconds(SEEK_TIMEOUT_S, self._on_seek_timeout)
        if self._state == "ended":
            self._playbin.set_state(Gst.State.PLAYING if self._want_play else Gst.State.PAUSED)
            self._set_state("playing" if self._want_play else "paused")

    def _on_seek_timeout(self) -> bool:
        self._seek_guard = 0
        if self._seek_busy:
            self._fail("no-range", "Jumping in this recording did not work")
        return False

    def _on_open_timeout(self) -> bool:
        self._open_guard = 0
        if not self._prerolled:
            self._fail("network" if self._uri.startswith("http") else "other",
                       "The recording did not start")
        return False

    def _on_tick(self) -> bool:
        if self._state in ("playing", "paused") and not self._seek_busy:
            self._show_position()
        # souphttpsrc retries a dead connection by itself and posts no error while
        # it does, so a camera that stops sending has to be noticed here.
        now = time.monotonic()
        waiting = self._state == "playing" or (
            self._buffering and self._want_play and self._state in ("paused", "opening"))
        if not waiting or self._frames != self._progress[0]:
            self._progress = (self._frames, now)
        elif now - self._progress[1] > STALL_TIMEOUT_S:
            self._timer = 0
            self._fail("network", "The camera stopped sending the recording")
            return False
        return True

    def _show_position(self, at_end: bool = False) -> None:
        position = self._duration if at_end and self._duration else self.position
        if position is None:
            return
        if not self._dragging and not self._seek_busy and self._seek_next is None:
            self._scale.set_value(position)
        self._time_label.set_text(f"{format_time(position)} / {format_time(self._duration)}")

    # "change-value" comes from the user only, never from set_value() above.
    def _on_scale_moved(self, _scale, _scroll, value: float) -> bool:
        self.seek(value, accurate=not self._dragging)  # key frames while dragging: quicker
        return False

    def _on_scale_pressed(self, *_args) -> bool:
        self._dragging = True
        return False

    def _on_scale_released(self, *_args) -> bool:
        self._dragging = False
        self.seek(self._scale.get_value())
        return False
