"""The recordings window against a fake camera, playing a short H.264 clip made on the spot.

Needs a display (the "gtk" fixture skips without one) and the x264enc and mp4mux
elements. One window serves all tests, as it serves the whole run of the app:
each test shows it for a camera of its own and it is hidden in between.
"""

import logging
import threading
from datetime import date, datetime

import pytest

from reolink_viewer import api
from reolink_viewer.config import Camera

from fake_reolink_api import FakeCamera, clip as recording

TODAY = date(2026, 11, 25)
SECONDS = 4
MOTION, PERSON, VEHICLE = "6D28808", "6D28C08", "6D28908"  # a person clip also has the motion flag


@pytest.fixture(scope="module")
def clip(gtk, tmp_path_factory) -> bytes:
    """A 4 s, 25 fps H.264 MP4 with its index at the end, as cameras write it."""
    from gi.repository import Gst

    if not all(Gst.ElementFactory.find(name) for name in ("x264enc", "h264parse", "mp4mux")):
        pytest.skip("x264enc, h264parse and mp4mux are needed to make a test clip")
    path = tmp_path_factory.mktemp("clip") / "clip.mp4"
    pipeline = Gst.parse_launch(
        f"videotestsrc num-buffers={SECONDS * 25} ! video/x-raw,width=320,height=240,framerate=25/1"
        " ! timeoverlay ! videoconvert ! x264enc key-int-max=25 speed-preset=ultrafast tune=zerolatency"
        f" ! h264parse ! mp4mux ! filesink location={path}")
    pipeline.set_state(Gst.State.PLAYING)
    message = pipeline.get_bus().timed_pop_filtered(
        30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipeline.set_state(Gst.State.NULL)
    assert message is not None and message.type == Gst.MessageType.EOS
    return path.read_bytes()


@pytest.fixture(scope="module")
def the_module(gtk):
    from reolink_viewer import recordings as module  # imports Gtk: only after the gtk fixture

    yield module
    window, module._window = module._window, None
    if window is not None:
        window.shut_down()
        window.destroy()


@pytest.fixture
def rec(the_module, monkeypatch, tmp_path):
    """The recordings module on a fixed day, with a clean API memory and a cache
    and a Videos folder of its own. The window is hidden again afterwards."""
    from reolink_viewer import apiui

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(the_module, "save_dir", lambda: tmp_path / "Videos" / "Reolink")
    monkeypatch.setattr(the_module, "_today", lambda: TODAY)
    for memory in (api._rejected, api._sessions, apiui._confirmed):
        memory.clear()
    yield the_module
    if the_module._window is not None:
        the_module._window.hide()
    for memory in (api._rejected, api._sessions, apiui._confirmed):
        memory.clear()


@pytest.fixture
def parent(gtk):
    window = gtk.Window(title="main window")
    window.show()
    yield window
    window.destroy()


class HeldCamera(FakeCamera):
    """A camera that answers a search only when the test lets it: the search stays in flight."""

    def __init__(self, **options):
        super().__init__(**options)
        self.search_arrived = threading.Event()
        self.let_go = threading.Event()

    def _answer(self, cmd: dict, token: str) -> dict:
        if cmd.get("cmd") == "Search":
            self.search_arrived.set()
            self.let_go.wait(10)
        return super()._answer(cmd, token)


def fill(fake: FakeCamera, hours=((8, MOTION), (12, PERSON), (17, VEHICLE)),
         earlier=(24, 19)) -> FakeCamera:
    """Clips today (three unless told otherwise) and one on each of some earlier
    days, all in both qualities, the Clear ones ten times the size."""
    for day, hour, flags in [(TODAY.day, *entry) for entry in hours] + [(day, 9, MOTION) for day in earlier]:
        for stream, size in (("sub", 3 * 1024 ** 2), ("main", 30 * 1024 ** 2)):
            entry = recording(datetime(TODAY.year, TODAY.month, day, hour), SECONDS, stream, flags)
            fake.add_clips({**entry, "size": str(size)})
    return fake


def camera(fake: FakeCamera, name: str = "Garage South", password: str = "pw") -> Camera:
    """The fake camera as the app's settings hold it: the same id every time it is asked for."""
    return Camera(name=name, host="127.0.0.1", password=password, api_port=fake.port, id=f"cam{fake.port}")


def open_window(rec, parent, fake: FakeCamera, run_loop, **options):
    """Show the window for the fake camera and wait for its first search to be answered."""
    rec.show_recordings(parent, camera(fake, **options))
    window = rec._window
    assert window.get_visible() and window.list_status.get_text() == rec.SEARCHING
    assert run_loop(lambda: window.list_status.get_text() != rec.SEARCHING)
    return window


def listed(window) -> list[str]:
    """The start times of the rows the list shows, top to bottom."""
    return [f"{row.recording.start:%H:%M}" for row in window.listbox.get_children()
            if row.get_child_visible()]


def filters(window) -> list[str]:
    return [name or "All" for name, button in window.filter_buttons.items() if button.get_visible()]


def texts(widget) -> set[str]:
    """Everything a widget says, in no order."""
    from gi.repository import Gtk

    if isinstance(widget, Gtk.Label):
        return {widget.get_text()}
    return {text for child in widget.get_children() for text in texts(child)}


def choose(window, start: str) -> None:
    """Click the row of the clip that started at `start` (HH:MM)."""
    row = next(row for row in window.listbox.get_children() if f"{row.recording.start:%H:%M}" == start)
    row.activate()


def playing(window, run_loop) -> bool:
    """Wait for the chosen clip to play, and see its position move on."""
    assert run_loop(lambda: window.player.state in ("playing", "error")), window.player.state
    assert window.player.state == "playing", window.cover_text.get_text()
    before = window.player.position
    return run_loop(lambda: window.player.position > before + 0.2, 5)


def press(window, key: str) -> None:
    from gi.repository import Gdk

    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.keyval = Gdk.keyval_from_name(key)
    window.emit("key-press-event", event)


def test_opening_marks_the_days_and_lists_today_newest_first(rec, parent, run_loop, caplog):
    caplog.set_level(logging.INFO, logger="reolink_viewer.recordings")
    with fill(FakeCamera()) as fake:
        window = open_window(rec, parent, fake, run_loop)
        assert window.get_title() == window.header.get_title() == "Recordings: Garage South"
        assert window.get_transient_for() is parent and not window.get_modal()
        assert [day for day in range(1, 31) if window.calendar.get_day_is_marked(day)] == [19, 24, 25]
        assert tuple(window.calendar.get_date()) == (2026, 10, 25)  # the calendar counts months from 0
        assert listed(window) == ["17:00", "12:00", "08:00"]
        # Fluent quality unless asked otherwise: the small files.
        assert window.quality_buttons["sub"].get_active()
        assert {row.recording.stream for row in window.listbox.get_children()} == {"sub"}
        row = window.listbox.get_row_at_index(1)
        assert texts(row) == {"12:00:00", "4 s", "Person · Motion", "3.0 MB"}
        quality = window.quality_buttons["main"].get_parent().get_parent()
        assert "Clear clips are large and use far more GPU memory" in quality.get_tooltip_text()
        # Nothing plays before a clip is chosen.
        assert window.player.state == "idle" and window.cover.get_visible()
        assert window.cover_text.get_text() == rec.CHOOSE
        assert window.heading.get_text() == "Wednesday 25 November 2026"
        assert not window.save_button.get_sensitive() and not window.next_button.get_sensitive()
        # One login; one search for the month's days, and two for the day (the
        # second asks whether more clips follow the last one found).
        assert fake.commands == ["Login", "Search", "Search", "Search"]
    assert "[Garage South] Fluent recordings on 2026-11-25: 3" in caplog.text


def test_opens_on_the_latest_day_with_recordings(rec, parent, run_loop, monkeypatch):
    monkeypatch.setattr(rec, "_today", lambda: date(2026, 11, 27))
    with fill(FakeCamera()) as fake:
        window = open_window(rec, parent, fake, run_loop)
        assert window.calendar.get_date().day == 25 and listed(window) == ["17:00", "12:00", "08:00"]
        assert window.heading.get_text() == "Wednesday 25 November 2026"


def test_an_empty_day_and_another_month_say_so(rec, parent, run_loop):
    with fill(FakeCamera()) as fake:
        window = open_window(rec, parent, fake, run_loop)
        window.calendar.select_day(20)  # as a click on the day does
        assert run_loop(lambda: window.list_status.get_text() == rec.SEARCHING)
        assert run_loop(lambda: window.list_status.get_text() == rec.NO_RECORDINGS)
        assert listed(window) == [] and window.cover_text.get_text() == rec.NO_RECORDINGS
        assert not window.filter_box.get_visible()
        searches = fake.commands.count("Search")
        assert searches == 4  # one more: the month's days are asked for once

        window.calendar.select_month(9, 2026)  # the arrows report the month, then the day
        window.calendar.select_day(20)
        assert run_loop(lambda: fake.commands.count("Search") == searches + 2)  # the month and the day
        assert run_loop(lambda: window.list_status.get_text() == rec.NO_RECORDINGS)
        assert not any(window.calendar.get_day_is_marked(day) for day in range(1, 32))
        run_loop(lambda: False, 0.2)
        assert fake.commands.count("Search") == searches + 2 and fake.login_attempts == 1


def test_the_quality_switch_lists_the_other_stream(rec, parent, run_loop):
    with fill(FakeCamera()) as fake:
        window = open_window(rec, parent, fake, run_loop)
        window.quality_buttons["main"].set_active(True)
        assert window.list_status.get_text() == rec.SEARCHING
        assert run_loop(lambda: listed(window) == ["17:00", "12:00", "08:00"])
        assert {row.recording.stream for row in window.listbox.get_children()} == {"main"}
        assert "30.0 MB" in texts(window.listbox.get_row_at_index(0))
        # The next camera starts with the small files again.
        with fill(FakeCamera()) as other:
            window = open_window(rec, parent, other, run_loop, name="Driveway")
            assert window.quality_buttons["sub"].get_active()
            assert {row.recording.stream for row in window.listbox.get_children()} == {"sub"}


def test_the_trigger_filter_narrows_the_list(rec, parent, run_loop):
    with fill(FakeCamera()) as fake:
        window = open_window(rec, parent, fake, run_loop)
        assert window.filter_box.get_visible()
        assert filters(window) == ["All", "Person", "Vehicle", "Motion"]  # no clip has an animal
        window.filter_buttons["Person"].set_active(True)
        assert listed(window) == ["12:00"]
        window.filter_buttons["Motion"].set_active(True)
        assert listed(window) == ["17:00", "12:00", "08:00"]
        window.filter_buttons["Vehicle"].set_active(True)
        assert listed(window) == ["17:00"]
        # A day with nothing of that kind: back to all clips, and with only one kind, no filters.
        window.calendar.select_day(24)
        assert run_loop(lambda: listed(window) == ["09:00"])
        assert window.filter_buttons[""].get_active()
        assert filters(window) == ["All", "Motion"]


def test_a_clip_plays_straight_from_a_camera_that_can_send_parts(
        rec, parent, clip, run_loop, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(rec, "SMALL_CLIP_BYTES", 0)  # the test clip is small: see the next test
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "12:00")
        assert window.cover.get_visible() and window.cover_text.get_text() == rec.OPENING
        assert window.heading.get_text() == "Wednesday 25 November 2026, 12:00:00"
        assert playing(window, run_loop)
        assert not window.cover.get_visible()
        assert window.listbox.get_selected_row().recording.start.hour == 12
        assert window.player.seekable and window.player.duration == pytest.approx(SECONDS, abs=0.1)
        assert window.save_button.get_sensitive()
        assert not list((rec.vod.cache_dir()).iterdir())  # nothing was downloaded
        assert fake.login_attempts == 1
        token = api.session_for(camera(fake)).playback_url(window._current).rsplit("token=", 1)[1]
    assert "playing it from the camera" in caplog.text and "picture after" in caplog.text
    assert "token=***" in caplog.text and token not in caplog.text


def test_a_small_clip_is_downloaded_even_then(rec, parent, clip, run_loop, caplog):
    caplog.set_level(logging.INFO, logger="reolink_viewer.recordings")
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "08:00")
        assert playing(window, run_loop)
    assert "downloading it first (a small clip" in caplog.text


def test_a_camera_that_only_sends_whole_files_gets_a_download(rec, parent, clip, run_loop, caplog):
    caplog.set_level(logging.INFO, logger="reolink_viewer.recordings")
    with fill(FakeCamera(clip_bytes=clip, ranges=False, rate=len(clip))) as fake:  # one second per clip
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "08:00")
        assert run_loop(lambda: window.progress.get_visible() and window.progress.get_fraction() > 0)
        assert window.cancel_button.get_visible() and window.cover.get_visible()
        assert window.cover_text.get_text().startswith(rec.DOWNLOADING)
        assert window.player.state == "idle"
        assert playing(window, run_loop)
        assert not window.cover.get_visible() and window.player.seekable
        assert [path.suffix for path in rec.vod.cache_dir().iterdir()] == [".clip"]
        assert "downloading it first (the camera only sends whole files" in caplog.text

        # Chosen again, it plays from the cache: the camera isn't asked a second time.
        asked = len(fake.paths)
        choose(window, "12:00")
        choose(window, "08:00")
        assert playing(window, run_loop)
        assert sum("source=" in path and "080000" in path for path in fake.paths[asked:]) == 0


def test_a_download_of_unknown_size_still_shows_it_is_busy(rec, parent, clip, run_loop):
    options = {"ranges": False, "content_length": False, "rate": len(clip)}
    with fill(FakeCamera(clip_bytes=clip, **options)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "08:00")
        assert run_loop(lambda: window.cover_text.get_text().endswith(" KB"))
        assert " of " not in window.cover_text.get_text()
        assert window.progress.get_visible() and window.cancel_button.get_visible()
        assert playing(window, run_loop)


def test_cancel_stops_a_download(rec, parent, clip, run_loop):
    with fill(FakeCamera(clip_bytes=clip, ranges=False, rate=len(clip) // 20)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "08:00")
        assert run_loop(window.cancel_button.get_visible)
        window.cancel_button.clicked()
        assert window.cover_text.get_text() == rec.CANCELLED and not window.cancel_button.get_visible()
        assert not window.progress.get_visible()
        run_loop(lambda: False, 0.5)
        assert window.player.state == "idle" and not list(rec.vod.cache_dir().glob("*.clip"))
        assert window.cover_text.get_text() == rec.CANCELLED


def test_a_rejected_login_is_shown_and_tried_exactly_once(rec, parent, run_loop):
    with fill(FakeCamera(password="right")) as fake:
        window = open_window(rec, parent, fake, run_loop, password="wrong")
        refusal = "The camera rejected the user name or password"
        assert window.list_status.get_text().startswith(refusal)
        assert window.cover_text.get_text().startswith(refusal)
        assert listed(window) == [] and fake.login_attempts == 1

        # Clicking around asks the camera nothing more: the refusal is remembered.
        def settled() -> bool:
            run_loop(lambda: window.list_status.get_text() == rec.SEARCHING, 0.5)
            return run_loop(lambda: window.list_status.get_text().startswith(refusal))

        window.calendar.select_day(24)
        assert settled()
        window.quality_buttons["main"].set_active(True)
        assert settled()
        window.calendar.select_month(9, 2026)
        assert settled()
        rec.show_recordings(parent, camera(fake, password="wrong"))
        window.hide()
        rec.show_recordings(parent, camera(fake, password="wrong"))
        assert settled()
        run_loop(lambda: False, 0.3)
        assert fake.login_attempts == 1 and fake.commands == ["Login"]


def test_an_unreachable_camera_is_explained_and_not_retried(rec, parent, run_loop):
    with FakeCamera() as fake:
        port = fake.port
    rec.show_recordings(parent, Camera(name="Gone", host="127.0.0.1", password="pw", api_port=port))
    window = rec._window
    assert run_loop(lambda: window.list_status.get_text() != rec.SEARCHING)
    assert window.list_status.get_text().startswith("Couldn't reach the camera's web interface")
    run_loop(lambda: False, 0.3)
    assert window.list_status.get_text().startswith("Couldn't reach")


def test_switching_camera_during_a_search_shows_only_the_new_cameras_clips(rec, parent, clip, run_loop):
    with fill(HeldCamera()) as slow, fill(FakeCamera(), ((6, PERSON), (7, PERSON)), earlier=(3,)) as quick:
        rec.show_recordings(parent, camera(slow, name="Slow"))
        window = rec._window
        assert run_loop(slow.search_arrived.is_set)
        rec.show_recordings(parent, camera(quick, name="Quick"))
        assert run_loop(lambda: listed(window) == ["07:00", "06:00"])
        slow.let_go.set()  # the first camera answers at last
        assert run_loop(lambda: slow.commands.count("Search") == 3)
        run_loop(lambda: False, 0.3)
        assert listed(window) == ["07:00", "06:00"]
        assert window.header.get_title() == "Recordings: Quick"
        assert [day for day in range(1, 31) if window.calendar.get_day_is_marked(day)] == [3, 25]


def test_asking_for_the_shown_camera_again_changes_nothing(rec, parent, clip, run_loop):
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "08:00")
        assert playing(window, run_loop)
        searches = fake.commands.count("Search")
        rec.show_recordings(parent, camera(fake))
        assert rec._window is window and window.player.state == "playing"
        assert fake.commands.count("Search") == searches


def test_clips_follow_each_other_and_the_buttons_and_keys_step_through_them(rec, parent, clip, run_loop):
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        assert window.autoplay.get_active()
        choose(window, "08:00")
        assert playing(window, run_loop)
        assert window.next_button.get_sensitive() and not window.previous_button.get_sensitive()
        window.player.seek(SECONDS - 0.5)
        # At its end the clip recorded after it starts, by itself.
        assert run_loop(lambda: window._current.start.hour == 12)
        assert playing(window, run_loop)
        assert window.listbox.get_selected_row().recording.start.hour == 12
        assert window.heading.get_text().endswith("12:00:00")

        window.next_button.clicked()
        assert window._current.start.hour == 17 and playing(window, run_loop)
        assert not window.next_button.get_sensitive() and window.previous_button.get_sensitive()
        window.previous_button.clicked()
        assert window._current.start.hour == 12
        # The arrow keys follow the list as drawn, newest first: down is the earlier clip.
        press(window, "Down")
        assert window._current.start.hour == 8
        press(window, "Up")
        press(window, "Up")
        assert window._current.start.hour == 17 and playing(window, run_loop)

        # With the switch off, the last picture stays.
        window.autoplay.set_active(False)
        choose(window, "08:00")
        assert playing(window, run_loop)
        window.player.seek(SECONDS - 0.5)
        assert run_loop(lambda: window.player.state == "ended")
        run_loop(lambda: False, 0.3)
        assert window._current.start.hour == 8 and window.player.state == "ended"
        assert not window.cover.get_visible()
        # And the day's last clip has nothing after it.
        window.autoplay.set_active(True)
        choose(window, "17:00")
        assert playing(window, run_loop)
        window.player.seek(SECONDS - 0.5)
        assert run_loop(lambda: window.player.state == "ended")
        assert window._current.start.hour == 17
        assert fake.login_attempts == 1


def test_stepping_keeps_to_the_filtered_clips(rec, parent, clip, run_loop):
    with fill(FakeCamera(clip_bytes=clip), hours=((8, PERSON), (12, VEHICLE), (17, PERSON))) as fake:
        window = open_window(rec, parent, fake, run_loop)
        window.filter_buttons["Person"].set_active(True)
        choose(window, "08:00")
        assert playing(window, run_loop)
        window.next_button.clicked()
        assert window._current.start.hour == 17 and not window.next_button.get_sensitive()
        # A filter that hides the clip being played leaves it playing, without a row.
        window.filter_buttons["Vehicle"].set_active(True)
        assert window._current.start.hour == 17 and window.listbox.get_selected_row() is None
        assert window.previous_button.get_sensitive()
        press(window, "Down")
        assert window._current.start.hour == 12


def at(window, run_loop, seconds: float) -> bool:
    """Wait for the clip to stand at `seconds` (while it jumps, the player knows no position)."""
    def there() -> bool:
        position = window.player.position
        return position is not None and abs(position - seconds) < 0.05

    return run_loop(there, 5)


def test_space_pauses_and_the_arrows_jump_five_seconds(rec, parent, clip, run_loop, monkeypatch):
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        press(window, "space")  # nothing chosen: nothing happens
        press(window, "Right")
        assert window.player.state == "idle"
        choose(window, "08:00")
        assert playing(window, run_loop)
        press(window, "space")
        assert window.player.state == "paused"
        window.player.seek(1.0)
        assert at(window, run_loop, 1.0)
        press(window, "Right")  # past the end of a 4 s clip: its last picture
        assert at(window, run_loop, SECONDS - 0.05)
        press(window, "Left")
        assert at(window, run_loop, 0.0)

        # Presses that come faster than the player can jump all count.
        monkeypatch.setattr(rec, "SEEK_STEP_S", 1)
        window.player.seek(0.5)
        assert at(window, run_loop, 0.5)
        for _ in range(3):
            press(window, "Right")
        assert at(window, run_loop, 3.5)
        run_loop(lambda: False, 0.3)
        press(window, "Left")
        press(window, "Left")
        assert at(window, run_loop, 1.5)
        press(window, "space")
        assert window.player.state == "playing"


def test_hiding_stops_the_clip_and_the_window_comes_back(rec, parent, clip, run_loop, monkeypatch):
    monkeypatch.setattr(rec, "SMALL_CLIP_BYTES", 0)
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "08:00")
        assert playing(window, run_loop)
        press(window, "Escape")
        assert not window.get_visible() and window.player.state == "idle" and window.player.position is None
        run_loop(lambda: False, 0.3)
        assert window.player.state == "idle"

        # Closing the window hides it too; it is the same window, and it plays again.
        window = open_window(rec, parent, fake, run_loop)
        assert window is rec._window and listed(window) == ["17:00", "12:00", "08:00"]
        assert window.cover_text.get_text() == rec.CHOOSE and window.listbox.get_selected_row() is None
        choose(window, "12:00")
        assert playing(window, run_loop)
        assert window.emit("delete-event", None) is True  # handled: not destroyed
        assert not window.get_visible() and window.player.state == "idle"
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "12:00")
        assert playing(window, run_loop)
        assert fake.login_attempts == 1


def test_hiding_cancels_a_download_and_a_search(rec, parent, clip, run_loop):
    with fill(FakeCamera(clip_bytes=clip, ranges=False, rate=len(clip) // 20)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "08:00")
        assert run_loop(window.cancel_button.get_visible)
        window.hide()
        run_loop(lambda: False, 0.5)
        assert not list(rec.vod.cache_dir().iterdir()) and window.player.state == "idle"
    with fill(HeldCamera()) as slow:
        rec.show_recordings(parent, camera(slow))
        assert run_loop(slow.search_arrived.is_set)
        window.hide()
        slow.let_go.set()
        assert run_loop(lambda: slow.commands.count("Search") == 3)
        run_loop(lambda: False, 0.3)
        assert listed(window) == [] and not window.get_visible()


def test_save_a_copy(rec, parent, clip, run_loop, monkeypatch):
    folder = rec.save_dir()
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "08:00")  # small, so downloaded for playing: the copy comes from the cache
        assert playing(window, run_loop)
        asked = len(fake.paths)
        window.save_button.clicked()
        assert not window.save_button.get_sensitive() and window.save_status.get_text() == rec.SAVING
        assert run_loop(lambda: window.save_status.get_text().startswith("Saved as "))
        first = folder / "Garage_South_20261125_080000.mp4"
        assert first.read_bytes() == clip and window.save_status.get_text().endswith(first.name)
        assert len(fake.paths) == asked and window.save_button.get_sensitive()
        assert window.player.state == "playing"  # saving doesn't disturb the clip

        # Saved once more, the first copy stays as it is.
        window.save_button.clicked()
        assert run_loop(lambda: window.save_status.get_text().endswith("_2.mp4"))
        assert sorted(path.name for path in folder.iterdir()) == [
            first.name, "Garage_South_20261125_080000_2.mp4"]

        # A clip played straight from the camera is fetched for the copy.
        monkeypatch.setattr(rec, "SMALL_CLIP_BYTES", 0)
        choose(window, "12:00")
        assert playing(window, run_loop)
        window.save_button.clicked()
        assert run_loop(lambda: window.save_status.get_text().startswith("Saved as "))
        assert (folder / "Garage_South_20261125_120000.mp4").read_bytes() == clip

        # Hiding the window deletes nothing that was saved.
        window.hide()
        assert len(list(folder.iterdir())) == 3 and window.save_status.get_text() == ""


@pytest.mark.parametrize("small", [0, 10 ** 9], ids=["fetched from the camera", "copied from the cache"])
def test_a_failed_copy_says_why(rec, parent, clip, run_loop, monkeypatch, tmp_path, small):
    blocked = tmp_path / "a file, not a folder"
    blocked.write_text("")
    monkeypatch.setattr(rec, "save_dir", lambda: blocked / "Reolink")
    monkeypatch.setattr(rec, "SMALL_CLIP_BYTES", small)
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "08:00")
        assert playing(window, run_loop)
        window.save_button.clicked()
        assert run_loop(lambda: window.save_status.get_text() != rec.SAVING)
        assert window.save_status.get_text() == "Couldn't save the recording (Not a directory)."
        assert window.save_button.get_sensitive()


def test_a_camera_that_stops_sending_parts_gets_one_download(
        rec, parent, clip, run_loop, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="reolink_viewer.recordings")
    monkeypatch.setattr(rec, "SMALL_CLIP_BYTES", 0)
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "08:00")
        assert playing(window, run_loop) and window._via == "stream"
        fake.ranges = False  # from now on the camera sends the whole file whatever is asked for
        window.player.seek(3.0)
        assert run_loop(lambda: window._via == "download")
        assert window.player.state == "idle" and window.player.status == ""
        assert window.cover_text.get_text().startswith(rec.DOWNLOADING)
        assert playing(window, run_loop)
        assert not window.cover.get_visible() and window.player.seekable
        window.player.pause()
        window.player.seek(3.0)  # in the file, jumping works
        assert at(window, run_loop, 3.0)
    assert "playing it from the camera failed" in caplog.text


def test_an_edited_camera_closes_the_window(rec, parent, run_loop):
    with fill(FakeCamera()) as fake, fill(FakeCamera()) as other:
        window = open_window(rec, parent, fake, run_loop)
        rec.close_recordings(camera(other).id)
        assert window.get_visible()
        rec.close_recordings(camera(fake).id)
        assert not window.get_visible()


def test_a_clip_the_camera_refuses_is_explained(rec, parent, clip, run_loop):
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        fake.refuse_playback = "file not exist"  # e.g. the SD card overwrote it meanwhile
        choose(window, "08:00")
        assert run_loop(lambda: window.cover_text.get_text() != rec.OPENING)
        assert window.cover_text.get_text() == "The camera refused to send the recording (file not exist)."
        assert window.cover.get_visible() and window.player.state == "idle"
        run_loop(lambda: False, 0.3)
        assert fake.login_attempts == 1  # nothing is tried again by itself
        fake.refuse_playback = None
        choose(window, "08:00")  # choosing it again is the user's move
        assert playing(window, run_loop)
        assert fake.login_attempts == 1


def test_a_camera_that_dropped_the_session_still_plays_the_clip(rec, parent, clip, run_loop, caplog):
    caplog.set_level(logging.INFO, logger="reolink_viewer.recordings")
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        fake.tokens.clear()  # the camera restarted while the list was showing
        choose(window, "12:00")
        assert playing(window, run_loop)
        assert fake.login_attempts == 2  # one new login, not one per refused request
    assert "logging in again" in caplog.text


def test_no_window_for_a_camera_the_user_declines(rec, parent, monkeypatch):
    from reolink_viewer import apiui

    asked = []
    monkeypatch.setattr(apiui, "ensure_login_allowed", lambda window, cam: asked.append(cam) and False)
    shown = rec._window is not None and rec._window.get_visible()
    rec.show_recordings(parent, Camera(name="Driveway", protocol="battery", uid="X"))
    assert len(asked) == 1
    assert (rec._window is not None and rec._window.get_visible()) == shown is False


def test_shutdown_stops_everything_and_empties_the_cache(rec, parent, clip, run_loop):
    with fill(FakeCamera(clip_bytes=clip)) as fake:
        window = open_window(rec, parent, fake, run_loop)
        choose(window, "08:00")
        assert playing(window, run_loop)
        assert list(rec.vod.cache_dir().iterdir())
        try:
            rec.shutdown()
            assert not window.get_visible() and window.player.state == "idle"
            assert not list(rec.vod.cache_dir().iterdir())
            rec.shutdown()  # safe to call again
            assert rec._window is None
        finally:
            window.destroy()
