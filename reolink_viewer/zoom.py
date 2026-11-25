"""The zoom and focus window for cameras with a motorised lens.

It stays open next to the live picture, so the user sees what each change
does. The camera takes absolute positions and needs a few seconds per move, so
only the position the user settles on is sent: one command at a time, the
newest choice winning, and the lens is read back once it has had time to stop.

Every camera call goes through the shared API session (api.session_for) on a
worker thread. Nothing here repeats a call by itself: a failure is shown, and
trying again is the user's decision (see the login lockout notes in api.py).
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import api, apiui, tasks  # noqa: E402
from .config import Camera  # noqa: E402

log = logging.getLogger(__name__)

# How long after the last key press, scroll step or [−] [+] click a position counts as chosen.
SETTLE_MS = 400
# The lens needs a few seconds, and with autofocus on the camera refocuses after
# a zoom move. reolink_aio waits this long before it reads the positions back.
READBACK_DELAY_MS = 3000

READING = "Reading the lens position…"
MOVING = "Moving…"
DONE = "Done"
NOT_THERE_YET = "The lens hasn't arrived yet. Press Refresh to see where it is."
NO_LENS = "This camera has no motorised zoom or focus."
AUTOFOCUS_ON = "The camera refocuses by itself after zooming."
AUTOFOCUS_OFF = "After zooming, set the focus yourself."


class _Row:
    """One motor's controls: [−] slider [+] and the position as a number.

    Holds back while the user is still choosing: `target` is set and settled()
    called once, when a drag is released or SETTLE_MS after the last other change.
    """

    def __init__(self, what: str, settled: Callable[[], None]):
        self.what = what  # "zoom" or "focus"
        self.target: int | None = None  # chosen by the user, not sent to the camera yet
        self._settled = settled
        self._dragging = False
        self._changed = False  # moved by the user since the last settle
        self._missed: tuple[int, int, int] | None = None  # the camera's report that came mid-drag
        self._timer = 0

        self.label = Gtk.Label(label=what.capitalize(), xalign=0)
        self.minus = Gtk.Button(label="−")
        self.plus = Gtk.Button(label="+")
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.set_size_request(240, -1)
        self.number = Gtk.Label(xalign=1, width_chars=4)

        self._handler = self.scale.connect("value-changed", self._on_value_changed)
        self.scale.connect("button-press-event", self._on_press)
        self.scale.connect("button-release-event", self._on_release)
        # Another window took the pointer mid-drag: no release will come.
        self.scale.connect("grab-broken-event", self._on_release)
        self.minus.connect("clicked", self._step, -1)
        self.plus.connect("clicked", self._step, 1)

    def attach(self, grid: Gtk.Grid, top: int) -> None:
        for column, widget in enumerate((self.label, self.minus, self.scale, self.plus, self.number)):
            grid.attach(widget, column, top, 1, 1)

    @property
    def position(self) -> int:
        return int(round(self.scale.get_value()))

    def show(self, position: int, lo: int, hi: int) -> None:
        """Show what the camera reports, without asking it to move."""
        if self._changed or self.target is not None:
            return  # the user's choice is about to be sent; the lens is read again after that
        if self._dragging:
            self._missed = (position, lo, hi)  # don't pull the slider out of the user's hand
            return
        with self.scale.handler_block(self._handler):
            self.scale.set_range(lo, hi)
            self.scale.set_increments(1, max(1, (hi - lo) // 20))
            self.scale.set_value(position)
        self.number.set_text(str(position))

    def cancel(self) -> None:
        """Stop the settle timer: the window is closing, or a newer change or a drag decides."""
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _step(self, _button: Gtk.Button, direction: int) -> None:
        self.scale.set_value(self.scale.get_value() + direction)

    def _on_value_changed(self, _scale: Gtk.Scale) -> None:
        self.number.set_text(str(self.position))
        self._changed = True
        self._missed = None
        self.cancel()
        if not self._dragging:
            self._timer = GLib.timeout_add(SETTLE_MS, self._on_timer)

    def _on_press(self, *_args) -> bool:
        self._dragging = True
        self.cancel()  # the release decides now
        return False

    def _on_release(self, *_args) -> bool:
        self._dragging = False
        if self._changed:
            self._settle()
        elif self._missed is not None:  # held but not moved: catch up with the camera
            missed, self._missed = self._missed, None
            self.show(*missed)
        return False

    def _on_timer(self) -> bool:
        self._timer = 0
        self._settle()
        return False

    def _settle(self) -> None:
        self.cancel()
        self._changed = False
        self.target = self.position
        self._settled()


class ZoomFocusWindow(Gtk.Window):
    """Zoom and focus controls for one camera. Not modal, so the live picture in
    the main window stays usable (and visible) while adjusting."""

    def __init__(self, parent: Gtk.Window, cam: Camera):
        super().__init__(title=f"Zoom and focus – {cam.name}", transient_for=parent,
                         type_hint=Gdk.WindowTypeHint.DIALOG, destroy_with_parent=True, resizable=False)
        self.cam = cam
        self._api = api.session_for(cam)
        self._closed = False
        self._busy = False  # a call to the camera is in flight; the rest waits its turn
        self._read_wanted = False
        self._autofocus_wanted: bool | None = None  # set by the user, not sent yet
        self._settling = False  # a command went out and the lens hasn't been read back since
        self._readback = 0  # timer for that read
        self._asked: tuple[_Row, int] | None = None  # the last move sent, to compare that read with

        self.zoom = _Row("zoom", self._next)
        self.focus = _Row("focus", self._next)
        self.autofocus = Gtk.CheckButton(label="Autofocus", no_show_all=True)
        self._autofocus_handler = self.autofocus.connect("toggled", self._on_autofocus_toggled)
        self.autofocus_note = Gtk.Label(xalign=0, no_show_all=True)
        self.autofocus_note.get_style_context().add_class("dim-label")
        self.status = Gtk.Label(xalign=0, wrap=True, max_width_chars=40, hexpand=True)
        self.refresh_button = Gtk.Button(label="Refresh", valign=Gtk.Align.END)
        self.refresh_button.connect("clicked", lambda _button: self.refresh())
        self.no_lens = Gtk.Label(label=NO_LENS, no_show_all=True)

        # Nothing to adjust until the camera has told its ranges and positions.
        self.controls = Gtk.Grid(column_spacing=8, row_spacing=8, sensitive=False)
        self.zoom.attach(self.controls, 0)
        self.focus.attach(self.controls, 1)
        self.controls.attach(self.autofocus, 1, 2, 4, 1)
        self.controls.attach(self.autofocus_note, 1, 3, 4, 1)
        footer = Gtk.Box(spacing=12)
        footer.pack_start(self.status, True, True, 0)
        footer.pack_end(self.refresh_button, False, False, 0)
        self._panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._panel.add(self.controls)
        self._panel.add(footer)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=16)
        box.add(self._panel)
        box.add(self.no_lens)
        self.add(box)
        self.connect("destroy", self._on_destroy)

    def refresh(self) -> None:
        """Read the lens position from the camera, once."""
        self._read_wanted = True
        self._next()

    # ------------------------------------------------------- one call at a time

    def _next(self) -> None:
        """Start what is waiting, if no call is in flight: the newest position for
        each motor, then the autofocus switch, then a read. Never more than one
        of each waits, so nothing piles up while the lens is busy."""
        if self._busy or self._closed:
            return
        for row in (self.zoom, self.focus):
            if row.target is not None:
                position, row.target = row.target, None
                self._move(row, position)
                return
        if self._autofocus_wanted is not None:
            enabled, self._autofocus_wanted = self._autofocus_wanted, None
            self._set_autofocus(enabled)
        elif self._read_wanted:
            self._read_wanted = False
            self._read()

    def _call(self, work: Callable[[], object], done: Callable[[object], None],
              failed: Callable[[Exception], None]) -> None:
        """Run one camera call on a worker thread. A closed window ignores the answer."""
        self._busy = True

        def finish(callback: Callable, value: object) -> None:
            if self._closed:
                return
            self._busy = False
            callback(value)
            self._next()

        tasks.run_async(work, lambda result: finish(done, result), lambda exc: finish(failed, exc))

    def _failed(self, exc: Exception) -> None:
        """Say what went wrong and drop what was waiting: trying again is up to the user."""
        text = apiui.explain(exc)
        log.warning("[%s] zoom and focus: %s", self.cam.name, text)
        self._stop_readback()
        self._read_wanted = False
        self._autofocus_wanted = None
        self.zoom.target = self.focus.target = None
        self._settling = False
        self.status.set_text(text)

    # ------------------------------------------------------------------ reading

    def _read(self) -> None:
        if not self._settling:
            self.status.set_text(READING)
        self._call(self._api.zoom_focus, self._on_state, self._on_read_failed)

    def _on_state(self, state: api.ZoomFocus) -> None:
        self.controls.set_sensitive(True)
        self.zoom.show(state.zoom, state.zoom_min, state.zoom_max)
        self.focus.show(state.focus, state.focus_min, state.focus_max)
        if self._autofocus_wanted is None:
            self._show_autofocus(state.autofocus)
        if not self._settling:
            self.status.set_text("")
        elif not self._readback and not self._read_wanted:  # the lens had its time to stop before this read
            self._settling = False
            self.status.set_text(DONE if self._arrived(state) else NOT_THERE_YET)

    def _arrived(self, state: api.ZoomFocus) -> bool:
        """Whether the lens is where the last move sent it. A long move can take
        longer than the wait before the read; the sliders then show a position on
        the way, and saying "Done" would invite the next step from the wrong place."""
        if self._asked is None:
            return True
        row, position = self._asked
        return position == (state.zoom if row is self.zoom else state.focus)

    def _on_read_failed(self, exc: Exception) -> None:
        self._failed(exc)
        if isinstance(exc, api.NotSupported):
            self._panel.hide()
            self.no_lens.show()

    def _show_autofocus(self, enabled: bool | None) -> None:
        """Show the camera's autofocus switch; None hides it (the model has none)."""
        self.autofocus.set_visible(enabled is not None)
        self.autofocus_note.set_visible(enabled is not None)
        if enabled is not None:
            with self.autofocus.handler_block(self._autofocus_handler):
                self.autofocus.set_active(enabled)
            self.autofocus_note.set_text(AUTOFOCUS_ON if enabled else AUTOFOCUS_OFF)

    # ----------------------------------------------------------------- commands

    def _move(self, row: _Row, position: int) -> None:
        log.info("[%s] moving the %s to %d", self.cam.name, row.what, position)
        self._begin(MOVING)
        self._asked = (row, position)
        send = self._api.set_zoom if row is self.zoom else self._api.set_focus
        self._call(lambda: send(position), self._on_sent, self._failed)

    def _on_autofocus_toggled(self, button: Gtk.CheckButton) -> None:
        self._autofocus_wanted = button.get_active()
        self.autofocus_note.set_text(AUTOFOCUS_ON if self._autofocus_wanted else AUTOFOCUS_OFF)
        self._next()

    def _set_autofocus(self, enabled: bool) -> None:
        log.info("[%s] turning autofocus %s", self.cam.name, "on" if enabled else "off")
        self._begin(f"Turning autofocus {'on' if enabled else 'off'}…")
        self._call(lambda: self._api.set_autofocus(enabled), self._on_sent, self._failed)

    def _begin(self, status: str) -> None:
        """A command is going out: the lens is read after it, not before."""
        self._stop_readback()
        self._read_wanted = False
        self._settling = True
        self._asked = None
        self.status.set_text(status)

    def _on_sent(self, _result: object) -> None:
        """The camera took the command: give the lens time to stop, then read it once."""
        self._readback = GLib.timeout_add(READBACK_DELAY_MS, self._on_readback)

    def _on_readback(self) -> bool:
        self._readback = 0
        self.refresh()
        return False

    def _stop_readback(self) -> None:
        if self._readback:
            GLib.source_remove(self._readback)
            self._readback = 0

    def _on_destroy(self, _window: Gtk.Window) -> None:
        self._closed = True
        self._stop_readback()
        self.zoom.cancel()
        self.focus.cancel()


_windows: dict[str, ZoomFocusWindow] = {}  # the open window of each camera, by camera id


def show_zoom_focus(parent: Gtk.Window, cam: Camera) -> None:
    """Open the camera's zoom and focus window, or bring the one that is open to the front."""
    window = _windows.get(cam.id)
    if window is not None and window.cam == cam:
        window.present()
        return
    close_zoom_focus(cam.id)  # the camera was edited since: start over with its new settings
    if not apiui.ensure_login_allowed(parent, cam):
        return
    window = _windows[cam.id] = ZoomFocusWindow(parent, cam)
    window.connect("destroy", _forget, cam.id)
    window.show_all()
    window.refresh()


def close_zoom_focus(cam_id: str) -> None:
    """Close a camera's zoom and focus window, if open (the camera was edited or removed)."""
    window = _windows.get(cam_id)
    if window is not None:
        window.destroy()


def close_all() -> None:
    """Close every zoom and focus window (the app is exiting)."""
    for cam_id in list(_windows):
        close_zoom_focus(cam_id)


def _forget(window: ZoomFocusWindow, cam_id: str) -> None:
    if _windows.get(cam_id) is window:
        del _windows[cam_id]
