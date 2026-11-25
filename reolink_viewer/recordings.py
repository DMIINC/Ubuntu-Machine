"""The recordings window: pick a day on the calendar, see its clips, play them one after another.

One window serves the whole run of the app. Closing it only hides it, and
asking for another camera's recordings switches what it shows: the GPU video
output of its ClipPlayer stays bound to this window's surface and must never
be freed or moved (see the notes on ClipPlayer).

Every camera call runs on a worker thread (tasks.run_async), and one click
leads to one chain of calls, so to one login attempt at most. Nothing here
repeats a call by itself: a failure is shown as text, and trying again is the
user's decision (see the login lockout notes in api.py). Answers that arrive
after the user has moved on to another camera, day or clip are dropped.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from . import api, apiui, clip_player, tasks, vod  # noqa: E402
from .config import Camera  # noqa: E402

log = logging.getLogger(__name__)

# The Quality switch: the camera's stream, and what Reolink's own app calls it.
QUALITIES = {"sub": "Fluent", "main": "Clear"}
QUALITY_TIP = "Fluent clips are small and start quickly. Clear clips are large and use far more GPU memory."
FILTERS = ("Person", "Vehicle", "Animal", "Motion")
SEEK_STEP_S = 5
LIST_WIDTH = 360  # pixels: the five filter buttons side by side
# A clip up to this size is downloaded even from a camera that could play it
# directly: GStreamer reads that much of an MP4 before the first picture anyway
# (see vod.py), and a download shows how far it is.
SMALL_CLIP_BYTES = 4 * 1024 * 1024

BUSY = -1.0  # as a download's fraction: under way, how far is not known

SEARCHING = "Searching the camera's storage…"
NO_RECORDINGS = "No recordings on this day"
CHOOSE = "Choose a recording from the list"
OPENING = "Opening…"
DOWNLOADING = "Downloading…"
CANCELLED = "Cancelled"
SAVING = "Saving a copy…"

CSS = b"""
.recordings-cover { background-color: #1a1a1a; color: #eeeeee; }
.recordings-cover-text { font-size: 120%; }
.recordings-heading { font-size: 130%; font-weight: bold; }
"""


def _today() -> date:
    """The day the window opens on (a function, so tests can name the day)."""
    return date.today()


def save_dir() -> Path:
    """Where "Save a copy…" puts recordings."""
    videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
    return Path(videos or Path.home() / "Videos") / "Reolink"


def save_path(cam: Camera, rec: api.Recording) -> Path:
    """A file name for a copy of the clip that isn't taken yet: saving never
    replaces something the user kept (the same clip in the other quality, say)."""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cam.name)
    stem = f"{safe}_{rec.start:%Y%m%d_%H%M%S}"
    suffix = ".mp4" if rec.name else ".flv"  # NVRs send their clips as FLV
    path = save_dir() / f"{stem}{suffix}"
    count = 1
    while path.exists():
        count += 1
        path = save_dir() / f"{stem}_{count}{suffix}"
    return path


def size_text(size: int | None) -> str:
    """A file size in words; "" if it isn't known."""
    if not size or size < 0:
        return ""
    if size < 1024 ** 2:
        return f"{max(size // 1024, 1)} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def length_text(seconds: float) -> str:
    """How long a clip runs, e.g. "45 s", "1 min 5 s", "1 h 2 min"."""
    minutes, secs = divmod(int(seconds + 0.5), 60)
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60} min"
    if minutes:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    return f"{secs} s"


def _day_text(day: date) -> str:
    return f"{day:%A} {day.day} {day:%B %Y}"


def _short(path: Path) -> str:
    """A path as the user knows it: ~/Videos/…"""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _copy(source: Path, target: Path) -> Path:
    """Copy a downloaded recording out of the cache. `target` gets its name only
    once it is complete, like a download's."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f"{target.name}.part")
    try:
        shutil.copyfile(source, part)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return target


def _address(session: api.CameraApi, rec: api.Recording) -> tuple[str, vod.Probe]:
    """Where to fetch a clip, and how the camera serves it (runs on a worker thread).

    A camera that restarted, or a session that ran out while the computer slept,
    answers "please login first" to a playback address. Then the session is
    renewed once (that login was accepted before) and the clip asked for again.
    Two clips refused at once share one renewal: the session logs in under its
    lock, and only if nobody else has since.
    """
    url = session.playback_url(rec)
    probe = vod.probe(url)
    if not probe.ok and probe.kind == "camera" and "login" in probe.text.lower():
        session.forget_token(url)
        fresh = session.playback_url(rec)
        if fresh != url:
            log.info("[%s] the camera dropped the session: asking again with a new one", session.cam.name)
            url, probe = fresh, vod.probe(fresh)
    return url, probe


def _disk_problem(exc: Exception) -> str:
    """Why a copy couldn't be written, in the words a failed download uses."""
    return f"Couldn't save the recording ({getattr(exc, 'strerror', None) or exc})."


class _Row(Gtk.ListBoxRow):
    """One clip in the day's list: when it started and what set it off, how long and how big it is."""

    def __init__(self, rec: api.Recording):
        super().__init__()
        self.recording = rec
        start = Gtk.Label(xalign=0, hexpand=True)
        start.set_markup(f"<b>{rec.start:%H:%M:%S}</b>")
        length = Gtk.Label(label=length_text(rec.duration.total_seconds()), xalign=1)
        triggers = Gtk.Label(label=" · ".join(rec.triggers), xalign=0, ellipsize=Pango.EllipsizeMode.END)
        size = Gtk.Label(label=size_text(rec.size), xalign=1)
        size.get_style_context().add_class("dim-label")
        grid = Gtk.Grid(column_spacing=12, row_spacing=2, margin=8)
        grid.attach(start, 0, 0, 1, 1)
        grid.attach(length, 1, 0, 1, 1)
        grid.attach(triggers, 0, 1, 1, 1)
        grid.attach(size, 1, 1, 1, 1)
        self.add(grid)
        self.show_all()


class RecordingsWindow(Gtk.Window):
    """Calendar, filters and the day's clips on the left; the player on the right.

    Made once (see show_recordings) and then only hidden and shown: hiding stops
    the clip and cancels downloads, and the player's video output stays where
    it first drew.
    """

    def __init__(self) -> None:
        super().__init__(title="Recordings")
        self.set_default_size(1280, 760)
        self.cam: Camera | None = None
        self._api: api.CameraApi | None = None
        self._stream = "sub"
        self._wanted = ""  # the filter: what must have triggered a clip, "" for any
        self._recordings: list[api.Recording] = []  # the day's clips, oldest first
        self._current: api.Recording | None = None  # the clip chosen (opening, playing or failed)
        self._marked: tuple[int, int, str] | None = None  # (year, month, stream) the calendar's marks are for
        # Counters that tell a late answer from a current one: each goes up when
        # the user leaves what was asked for.
        self._search = 0
        self._clip = 0
        self._save = 0
        self._updating = False  # widgets are being set by the program, not by the user
        self._reload_queued = 0
        self._url = ""  # of the current clip; carries the session token
        self._via = ""  # how the current clip gets here: "stream", "download" or "cache"
        self._fell_back = False
        self._chosen_at = 0.0
        self._job: vod.Download | None = None  # the current clip on its way to the cache
        self._saving = False
        self._save_job: vod.Download | None = None

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._build_header()
        body = Gtk.Box(spacing=12, margin=12)
        body.pack_start(self._build_list_side(), False, False, 0)
        body.pack_start(self._build_player_side(), True, True, 0)
        body.show_all()
        self.add(body)
        self._show_cover("")

        self.connect("delete-event", self._on_delete)
        self.connect("hide", self._on_hidden)
        self.connect("key-press-event", self._on_key)

    # ---------------------------------------------------------------- layout

    def _radio_row(self, labels: dict[str, str],
                   chosen: Callable[[str], None]) -> tuple[Gtk.Box, dict[str, Gtk.RadioButton]]:
        """Buttons of which one is pressed, joined into a strip. chosen(key) runs
        when the user presses another."""
        strip = Gtk.Box()
        strip.get_style_context().add_class("linked")
        buttons: dict[str, Gtk.RadioButton] = {}
        first = None
        for key, label in labels.items():
            button = Gtk.RadioButton.new_with_label_from_widget(first, label)
            button.set_mode(False)  # drawn as a button, not as a round check mark
            button.connect("toggled", self._on_radio_toggled, chosen, key)
            first = first or button
            buttons[key] = button
            strip.pack_start(button, True, True, 0)
        return strip, buttons

    def _on_radio_toggled(self, button: Gtk.RadioButton, chosen: Callable[[str], None], key: str) -> None:
        if button.get_active() and not self._updating:
            chosen(key)

    def _build_header(self) -> None:
        self.header = Gtk.HeaderBar(show_close_button=True, title="Recordings")
        self.set_titlebar(self.header)
        strip, self.quality_buttons = self._radio_row(QUALITIES, self._on_quality_chosen)
        quality = Gtk.Box(spacing=8)
        quality.pack_start(Gtk.Label(label="Quality"), False, False, 0)
        quality.pack_start(strip, False, False, 0)
        quality.set_tooltip_text(QUALITY_TIP)
        self.header.pack_end(quality)
        self.header.show_all()

    def _build_list_side(self) -> Gtk.Widget:
        # Not focusable: with the keyboard focus the calendar would take the
        # arrow keys and Space, which here belong to the player and the list.
        self.calendar = Gtk.Calendar(can_focus=False)
        self.calendar.connect("month-changed", self._on_calendar_changed)
        self.calendar.connect("day-selected", self._on_calendar_changed)

        self.filter_box, self.filter_buttons = self._radio_row(
            {"": "All", **{name: name for name in FILTERS}}, self._on_filter_chosen)
        self.filter_box.set_no_show_all(True)
        for button in self.filter_buttons.values():
            button.show_all()
            button.set_no_show_all(True)

        self.listbox = Gtk.ListBox()
        self.listbox.set_filter_func(lambda row: self._matches(row.recording))
        self.listbox.connect("row-activated", lambda _list, row: self._play(row.recording))
        # Shown in place of the rows while there are none: searching, nothing found, or what went wrong.
        self.list_status = Gtk.Label(wrap=True, max_width_chars=34, justify=Gtk.Justification.CENTER,
                                     margin=24)
        self.list_status.show()
        self.listbox.set_placeholder(self.list_status)
        self._scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
                                            shadow_type=Gtk.ShadowType.IN)
        self._scroller.add(self.listbox)

        # A fixed width: the column must not narrow when the filters are hidden, nor
        # (hexpand said outright) share the window's spare width with the player
        # because the labels in its rows expand.
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=False)
        side.set_size_request(LIST_WIDTH, -1)
        side.pack_start(self.calendar, False, False, 0)
        side.pack_start(self.filter_box, False, False, 0)
        side.pack_start(self._scroller, True, True, 0)
        return side

    def _build_player_side(self) -> Gtk.Widget:
        # Shortened at its start if the window is narrow: the time of day is what tells clips apart.
        self.heading = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.START)
        self.heading.get_style_context().add_class("recordings-heading")
        # In time, as clips follow each other when played; the list runs the other way (newest first).
        self.previous_button = Gtk.Button(label="Previous")
        self.previous_button.set_tooltip_text("The clip before this one (Down)")
        self.previous_button.connect("clicked", lambda _button: self._step(-1))
        self.next_button = Gtk.Button(label="Next")
        self.next_button.set_tooltip_text("The clip after this one (Up)")
        self.next_button.connect("clicked", lambda _button: self._step(1))
        top = Gtk.Box(spacing=6)
        top.pack_start(self.heading, True, True, 0)
        top.pack_start(self.previous_button, False, False, 0)
        top.pack_start(self.next_button, False, False, 0)

        self.player = clip_player.ClipPlayer()
        self.player.on_state = self._on_player_state
        self.player.on_ended = self._on_clip_ended
        self.player.on_error = self._on_player_error
        # The player keeps showing a clip's last frame after it is closed: while
        # there is no picture of the chosen clip, this lies over it and says why.
        self.cover = Gtk.EventBox(no_show_all=True)
        self.cover.get_style_context().add_class("recordings-cover")
        self.cover_text = Gtk.Label(wrap=True, max_width_chars=48, justify=Gtk.Justification.CENTER)
        self.cover_text.get_style_context().add_class("recordings-cover-text")
        self.progress = Gtk.ProgressBar(no_show_all=True)
        self.progress.set_size_request(320, -1)
        self.cancel_button = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER, no_show_all=True)
        self.cancel_button.connect("clicked", lambda _button: self._cancel())
        message = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=24,
                          halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        message.pack_start(self.cover_text, False, False, 0)
        message.pack_start(self.progress, False, False, 0)
        message.pack_start(self.cancel_button, False, False, 0)
        message.show_all()
        self.cover.add(message)
        # An overlay, so the player itself is packed once and never moved.
        overlay = Gtk.Overlay()
        overlay.add(self.player)
        overlay.add_overlay(self.cover)
        overlay.connect("get-child-position", self._place_cover)

        self.autoplay = Gtk.CheckButton(label="Play clips one after another", active=True)
        self.autoplay.set_tooltip_text("When a clip ends, the one recorded after it starts")
        self.save_status = Gtk.Label(xalign=1, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.save_button = Gtk.Button(label="Save a copy…")
        self.save_button.set_tooltip_text(f"Keep this clip in {_short(save_dir())}")
        self.save_button.connect("clicked", lambda _button: self._save_copy())
        bottom = Gtk.Box(spacing=12)
        bottom.pack_start(self.autoplay, False, False, 0)
        bottom.pack_start(self.save_status, True, True, 0)
        bottom.pack_start(self.save_button, False, False, 0)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        side.pack_start(top, False, False, 0)
        side.pack_start(overlay, True, True, 0)
        side.pack_start(bottom, False, False, 0)
        return side

    def _place_cover(self, overlay: Gtk.Overlay, _cover: Gtk.Widget, place: Gdk.Rectangle) -> bool:
        """Lay the cover over the picture only: the player's controls stay in view."""
        area = self.player.video_area
        corner = area.translate_coordinates(overlay, 0, 0)
        if corner is None:
            return False
        place.x, place.y = corner
        place.width, place.height = area.get_allocated_width(), area.get_allocated_height()
        return True

    # ------------------------------------------------------- showing, hiding

    def show_camera(self, parent: Gtk.Window, cam: Camera) -> None:
        """Show this camera's recordings in place of whatever the window showed,
        starting at today in Fluent quality."""
        if self.get_visible() and cam == self.cam:
            self.present()
            return
        self._leave()
        self.cam = cam
        self._api = api.session_for(cam)
        self.set_title(f"Recordings: {cam.name}")
        self.header.set_title(f"Recordings: {cam.name}")
        self.set_transient_for(parent)
        today = _today()
        self._updating = True
        self.calendar.select_month(today.month - 1, today.year)
        self.calendar.select_day(today.day)
        self.quality_buttons["sub"].set_active(True)
        self.filter_buttons[""].set_active(True)
        self._updating = False
        self._stream, self._wanted, self._marked = "sub", "", None
        self._load(pick_day=True)
        self.present()

    def shut_down(self) -> None:
        """For good, when the app exits."""
        self.hide()
        self.player.shutdown()

    def _on_delete(self, *_args) -> bool:
        self.hide()
        return True  # hidden, not destroyed: see the notes at the top

    def _on_hidden(self, _window: Gtk.Window) -> None:
        self._leave()

    def _leave(self) -> None:
        """Let go of all that belongs to what was shown: the clip and its download,
        a copy being saved, answers still on their way. Nothing plays or downloads
        behind a hidden window, and the player gives its GPU memory back."""
        if self._reload_queued:
            GLib.source_remove(self._reload_queued)
            self._reload_queued = 0
        self._search += 1
        self._stop_clip()
        self._save += 1
        if self._save_job is not None:
            self._save_job.cancel()
        self._end_save("")
        self._show_clips([], "")

    def _on_key(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
            return False
        key = event.keyval
        if key == Gdk.KEY_Escape:
            self.hide()
        elif key == Gdk.KEY_space:
            self.player.toggle()
        elif key in (Gdk.KEY_Left, Gdk.KEY_Right):
            self.player.skip(SEEK_STEP_S if key == Gdk.KEY_Right else -SEEK_STEP_S)
        elif key in (Gdk.KEY_Up, Gdk.KEY_Down):
            # As the list is drawn, newest first: up is the clip recorded after this one.
            self._step(1 if key == Gdk.KEY_Up else -1)
        else:
            return False
        return True

    # ------------------------------------------------------------- searching

    def _on_quality_chosen(self, stream: str) -> None:
        self._stream = stream
        self._load()

    def _on_calendar_changed(self, _calendar: Gtk.Calendar) -> None:
        if self._updating:
            return
        self._search += 1  # an answer still on its way is for a day the user has left
        # Stepping to another month reports the month and then the day: one search covers both.
        if not self._reload_queued:
            self._reload_queued = GLib.idle_add(self._reload)

    def _reload(self) -> bool:
        self._reload_queued = 0
        self._load()
        return False

    def _shown_day(self) -> date:
        year, month, day = self.calendar.get_date()
        return date(year, month + 1, day)  # the calendar counts months from 0

    def _load(self, pick_day: bool = False) -> None:
        """Search the camera's storage for the day the calendar shows and, for a
        month not looked up yet, for the days to mark: one worker task does both,
        so it logs in once at most, and its first failure ends it.

        pick_day (on opening): if that day has no recordings, move to the
        latest earlier day of the month that has.
        """
        self._stop_clip()
        self._search += 1
        search, session, stream, day = self._search, self._api, self._stream, self._shown_day()
        marks = self._marked != (day.year, day.month, stream)
        if marks:
            self.calendar.clear_marks()
            self._marked = None
        self._show_clips([], SEARCHING)

        def work() -> tuple[list[int] | None, date, list[api.Recording]]:
            days = session.recording_days(day.year, day.month, stream) if marks else None
            shown = day
            if pick_day and days and day.day not in days:
                shown = day.replace(day=max((d for d in days if d < day.day), default=day.day))
            return days, shown, session.recordings(shown, stream)

        tasks.run_async(work, lambda found: self._on_found(search, *found),
                        lambda exc: self._on_search_failed(search, exc))

    def _on_found(self, search: int, days: list[int] | None, day: date,
                  clips: list[api.Recording]) -> None:
        if search != self._search:
            return  # the user has moved on to another camera, day or quality
        if days is not None:
            self._marked = (day.year, day.month, self._stream)
            for marked in days:
                self.calendar.mark_day(marked)
        self._updating = True
        self.calendar.select_day(day.day)
        self._updating = False
        log.info("[%s] %s recordings on %s: %d", self.cam.name, QUALITIES[self._stream], day, len(clips))
        self._show_clips(clips, NO_RECORDINGS)

    def _on_search_failed(self, search: int, exc: Exception) -> None:
        if search != self._search:
            return
        text = apiui.explain(exc)
        log.warning("[%s] searching for recordings failed: %s", self.cam.name, text)
        self._show_clips([], text)

    # -------------------------------------------------------------- the list

    def _show_clips(self, clips: list[api.Recording], otherwise: str) -> None:
        """List the day's clips, newest first. `otherwise` is what the list, and the
        place of the picture, say while there are none."""
        self._recordings = clips
        for row in self.listbox.get_children():
            row.destroy()
        for rec in reversed(clips):
            self.listbox.add(_Row(rec))
        self.list_status.set_text(otherwise)
        # Only filters that would leave something: "All" alone is no choice, and
        # a filter kept from another day gives way if nothing here matches it.
        found = {trigger for rec in clips for trigger in rec.triggers}
        self._updating = True
        if clips and not any(self._matches(rec) for rec in clips):
            self._wanted = ""
            self.filter_buttons[""].set_active(True)
        for name, button in self.filter_buttons.items():
            button.set_visible(not name or name in found)
        self._updating = False
        self.filter_box.set_visible(any(name in found for name in FILTERS))
        self.listbox.invalidate_filter()
        self._show_heading()
        self._update_buttons()
        self._show_cover(CHOOSE if clips else otherwise)

    def _on_filter_chosen(self, trigger: str) -> None:
        self._wanted = trigger
        self.listbox.invalidate_filter()
        self._select_row()
        self._update_buttons()

    def _matches(self, rec: api.Recording) -> bool:
        return not self._wanted or self._wanted in rec.triggers

    def _neighbour(self, step: int) -> api.Recording | None:
        """The listed clip recorded after (step 1) or before (-1) the current one;
        with none chosen, the day's first or its last."""
        clips = [rec for rec in self._recordings if self._matches(rec)]
        if step < 0:
            clips.reverse()
        if self._current is not None:
            clips = [rec for rec in clips if (rec.start - self._current.start).total_seconds() * step > 0]
        return clips[0] if clips else None

    def _step(self, step: int) -> None:
        rec = self._neighbour(step)
        if rec is not None:
            self._play(rec)

    def _select_row(self) -> None:
        """Mark the current clip's row and bring it into view (it has none while a filter hides it)."""
        self.listbox.unselect_all()
        if self._current is None or not self._matches(self._current):
            return
        for row in self.listbox.get_children():
            if row.recording == self._current:
                self.listbox.select_row(row)
                place = row.get_allocation()
                self._scroller.get_vadjustment().clamp_page(place.y, place.y + place.height)
                return

    def _show_heading(self) -> None:
        """Above the picture: when the clip was recorded, or the day while none is chosen."""
        if self._current is not None:
            self.heading.set_text(f"{_day_text(self._current.start)}, {self._current.start:%H:%M:%S}")
        else:
            self.heading.set_text(_day_text(self._shown_day()))

    def _update_buttons(self) -> None:
        chosen = self._current is not None
        self.previous_button.set_sensitive(chosen and self._neighbour(-1) is not None)
        self.next_button.set_sensitive(chosen and self._neighbour(1) is not None)
        self.save_button.set_sensitive(chosen and not self._saving)

    # --------------------------------------------------------------- playing

    def _cached(self, rec: api.Recording) -> Path:
        return vod.cache_path(f"{self.cam.id} {self.cam.host} {self.cam.channel} {rec.key}")

    def _clip_name(self) -> str:
        return f"{QUALITIES.get(self._current.stream, self._current.stream)} clip of {self._current.start}"

    def _play(self, rec: api.Recording) -> None:
        """Start a clip. One downloaded earlier in this run plays from the cache;
        for any other the camera is asked where it is and how it can be played."""
        self._stop_clip()
        self._current = rec
        self._chosen_at = time.monotonic()
        self._fell_back = False
        self._show_heading()
        self._select_row()
        self._update_buttons()
        cached = self._cached(rec)
        if cached.is_file():
            self._via = "cache"
            self._open(cached.as_uri())
            return
        self._show_cover(OPENING)
        clip, session = self._clip, self._api

        tasks.run_async(lambda: _address(session, rec), lambda answer: self._on_probed(clip, *answer),
                        lambda exc: self._on_address_failed(clip, exc))

    def _on_address_failed(self, clip: int, exc: Exception) -> None:
        if clip == self._clip:
            self._clip_failed(apiui.explain(exc))

    def _on_probed(self, clip: int, url: str, probe: vod.Probe) -> None:
        if clip != self._clip:
            return  # the user has chosen another clip since
        if not probe.ok:
            self._clip_failed(probe.text)
            return
        self._url = url
        size = size_text(probe.size or self._current.size) or "size unknown"
        if not probe.ranges:
            self._download(f"the camera only sends whole files, {size}")
        elif probe.size is not None and probe.size <= SMALL_CLIP_BYTES:
            self._download(f"a small clip, {size}")
        elif self._api.legacy_tls:
            # GStreamer's HTTPS doesn't speak the old TLS of this camera's firmware.
            self._download(f"the camera only has old TLS, {size}")
        else:
            self._via = "stream"
            log.info("[%s] %s: playing it from the camera (%s)", self.cam.name, self._clip_name(), size)
            self._open(url)

    def _open(self, uri: str) -> None:
        self.player.open(uri, self._current.duration.total_seconds() or None, self.cam.audio)

    def _download(self, why: str) -> None:
        """Fetch the current clip into the cache, showing how far it is, then play the file."""
        self._via = "download"
        log.info("[%s] %s: downloading it first (%s)", self.cam.name, self._clip_name(), why)
        self._job = vod.Download(
            self._url, self._cached(self._current), GLib.idle_add, on_progress=self._on_progress,
            on_done=self._on_downloaded, on_failed=self._on_download_failed).start()
        self._show_cover(DOWNLOADING, 0.0)

    def _on_progress(self, done: int, total: int | None) -> None:
        if total:
            self._show_cover(f"{DOWNLOADING} {size_text(done)} of {size_text(total)}", done / total)
        else:  # the camera didn't say how much is coming
            self._show_cover(f"{DOWNLOADING} {size_text(done)}", BUSY)

    def _on_downloaded(self, path: Path) -> None:
        self._job = None
        self._open(path.as_uri())

    def _on_download_failed(self, _kind: str, text: str) -> None:
        self._job = None
        self._clip_failed(text)

    def _on_player_state(self, state: str) -> None:
        if state == "opening":
            if not self.cover.get_visible():  # played once more from its start
                self._chosen_at = time.monotonic()
            self._show_cover(OPENING)
        elif state in ("playing", "paused", "ended") and self.cover.get_visible():
            self.cover.hide()
            log.info("[%s] %s: picture after %.1f s (%s)", self.cam.name, self._clip_name(),
                     time.monotonic() - self._chosen_at, self._via)

    def _on_player_error(self, kind: str, text: str) -> None:
        if self._via == "stream" and kind in clip_player.NEEDS_DOWNLOAD and not self._fell_back:
            self._fell_back = True  # once: a downloaded file that fails too is reported
            self.player.stop()  # takes the error off the player's own status line
            self._download(f"playing it from the camera failed: {text}")
        else:
            if self._via in ("download", "cache") and kind in ("no-moov", "not-video", "other"):
                # A download can end early without anyone noticing (no length
                # given). Don't keep it: the next click fetches the clip again.
                self._cached(self._current).unlink(missing_ok=True)
            self._clip_failed(text)

    def _on_clip_ended(self) -> None:
        if self.autoplay.get_active():
            self._step(1)

    def _clip_failed(self, text: str) -> None:
        """Say why the current clip doesn't play. It stays chosen, and nothing is tried again."""
        log.warning("[%s] %s: %s", self.cam.name, self._clip_name(), text)
        self._show_cover(text)

    def _cancel(self) -> None:
        self._halt_clip()
        self._show_cover(CANCELLED)

    def _halt_clip(self) -> None:
        """Stop the current clip wherever it is: asked for, downloading or playing."""
        self._clip += 1
        if self._job is not None:
            self._job.cancel()
            self._job = None
        self.player.stop()

    def _stop_clip(self) -> None:
        """Stop the current clip and forget it."""
        self._halt_clip()
        self._current = None
        self._url = ""
        self.listbox.unselect_all()
        self._show_cover("")

    def _show_cover(self, text: str, fraction: float | None = None) -> None:
        """Cover the picture with a message; with a fraction (or BUSY), also a progress bar."""
        self.cover_text.set_text(text)
        self.progress.set_visible(fraction is not None)
        if fraction == BUSY:
            self.progress.pulse()
        elif fraction is not None:
            self.progress.set_fraction(min(fraction, 1.0))
        self.cancel_button.set_visible(self._job is not None)
        self.cover.show()

    # ---------------------------------------------------------- saving a copy

    def _save_copy(self) -> None:
        """Put the current clip in the user's Videos folder: copied from the cache
        if it was downloaded for playing, else fetched from the camera."""
        rec = self._current
        if rec is None or self._saving:
            return
        self._saving = True
        self._update_buttons()
        self._say_save(SAVING)
        save, session = self._save, self._api
        target, cached = save_path(self.cam, rec), self._cached(rec)
        if cached.is_file():
            tasks.run_async(lambda: _copy(cached, target), lambda path: self._on_saved(save, path),
                            lambda exc: self._on_save_failed(save, _disk_problem(exc)))
        else:
            tasks.run_async(lambda: _address(session, rec)[0],
                            lambda url: self._fetch_copy(save, url, target),
                            lambda exc: self._on_save_failed(save, apiui.explain(exc)))

    def _fetch_copy(self, save: int, url: str, target: Path) -> None:
        if save != self._save:
            return
        self._save_job = vod.Download(
            url, target, GLib.idle_add, on_progress=self._on_save_progress,
            on_done=lambda path: self._on_saved(save, path),
            on_failed=lambda _kind, text: self._on_save_failed(save, text)).start()

    def _on_save_progress(self, done: int, total: int | None) -> None:
        self._say_save(f"{SAVING} {size_text(done)} of {size_text(total)}" if total
                       else f"{SAVING} {size_text(done)}")

    def _on_saved(self, save: int, path: Path) -> None:
        if save != self._save:
            return
        log.info("[%s] saved a copy of a clip as %s", self.cam.name, path)
        self._end_save(f"Saved as {_short(path)}")

    def _on_save_failed(self, save: int, text: str) -> None:
        if save != self._save:
            return
        log.warning("[%s] saving a copy failed: %s", self.cam.name, text)
        self._end_save(text)

    def _end_save(self, text: str) -> None:
        self._saving = False
        self._save_job = None
        self._say_save(text)
        self._update_buttons()

    def _say_save(self, text: str) -> None:
        self.save_status.set_text(text)
        self.save_status.set_tooltip_text(text or None)  # a long path is shortened in the middle


_window: RecordingsWindow | None = None


def show_recordings(parent: Gtk.Window, cam: Camera) -> None:
    """Show the recordings on a camera's storage (from the camera's right-click menu)."""
    global _window
    if not apiui.ensure_login_allowed(parent, cam):
        return
    if _window is None:
        _window = RecordingsWindow()
    _window.show_camera(parent, cam)


def close_recordings(cam_id: str) -> None:
    """Hide the window if it shows this camera. For when the camera is edited or
    removed: what the window holds, its login above all, is then out of date."""
    if _window is not None and _window.cam is not None and _window.cam.id == cam_id:
        _window.hide()


def shutdown() -> None:
    """For when the app exits: stop playing, cancel what is being fetched, and
    delete the recordings downloaded for playing (saved copies stay)."""
    global _window
    if _window is not None:
        _window.shut_down()
        _window = None
    vod.clear_cache()
