"""The zoom and focus window against a fake camera. Needs a display (the "gtk" fixture skips without one)."""

import dataclasses
import logging
import threading

import pytest

from reolink_viewer import api
from reolink_viewer.config import Camera

from fake_reolink_api import FakeCamera


@pytest.fixture
def zoom(gtk, monkeypatch):
    """The zoom module with short delays, a clean API memory and no window left behind."""
    from reolink_viewer import apiui, zoom as module  # imports Gtk: only after the gtk fixture

    monkeypatch.setattr(module, "SETTLE_MS", 60)
    monkeypatch.setattr(module, "READBACK_DELAY_MS", 150)
    for memory in (api._rejected, api._locked, api._sessions, apiui._confirmed):
        memory.clear()
    yield module
    for window in list(module._windows.values()):
        window.destroy()
    for memory in (api._rejected, api._locked, api._sessions, apiui._confirmed):
        memory.clear()


@pytest.fixture
def parent(gtk):
    window = gtk.Window(title="main window")
    window.show()
    yield window
    window.destroy()


class HeldCamera(FakeCamera):
    """A camera that answers a move only when the test lets it: a command stays in flight."""

    def __init__(self):
        super().__init__()
        self.move_arrived = threading.Event()
        self.let_go = threading.Event()

    def _answer(self, cmd: dict, token: str) -> dict:
        if cmd.get("cmd") == "StartZoomFocus":
            self.move_arrived.set()
            self.let_go.wait(10)
        return super()._answer(cmd, token)


def camera(fake: FakeCamera, password: str = "pw") -> Camera:
    return Camera(name="Garage", host="127.0.0.1", password=password, api_port=fake.port)


def open_window(zoom, parent, fake: FakeCamera, run_loop, password: str = "pw"):
    """Open the window and wait for its first read to be answered."""
    cam = camera(fake, password)
    zoom.show_zoom_focus(parent, cam)
    window = zoom._windows[cam.id]
    assert window.status.get_text() == zoom.READING
    assert run_loop(lambda: window.status.get_text() != zoom.READING)
    return window


def press(row) -> None:
    from gi.repository import Gdk

    row.scale.emit("button-press-event", Gdk.Event.new(Gdk.EventType.BUTTON_PRESS))


def release(row) -> None:
    from gi.repository import Gdk

    row.scale.emit("button-release-event", Gdk.Event.new(Gdk.EventType.BUTTON_RELEASE))


def pause(run_loop, zoom, settles: float = 4) -> None:
    """Let the main loop run for a few settle times, so anything due would have happened."""
    run_loop(lambda: False, settles * zoom.SETTLE_MS / 1000)


def test_opening_reads_and_shows_ranges_and_positions(zoom, parent, run_loop):
    with FakeCamera() as fake:
        cam = camera(fake)
        zoom.show_zoom_focus(parent, cam)
        window = zoom._windows[cam.id]
        assert window.status.get_text() == zoom.READING
        assert not window.controls.get_sensitive()  # nothing to adjust before the ranges are known
        assert not window.get_modal() and window.get_transient_for() is parent

        assert run_loop(window.controls.get_sensitive)
        for row, position, top in ((window.zoom, 10, 33), (window.focus, 120, 223)):
            adjustment = row.scale.get_adjustment()
            assert (adjustment.get_lower(), adjustment.get_upper()) == (0, top)
            assert row.position == position and row.number.get_text() == str(position)
        assert window.autofocus.get_visible() and window.autofocus.get_active()
        assert window.autofocus_note.get_visible()
        assert window.autofocus_note.get_text() == "The camera refocuses by itself after zooming."
        assert window.status.get_text() == ""
        pause(run_loop, zoom)
        assert fake.moves == [] and fake.login_attempts == 1
        assert fake.commands.count("GetZoomFocus") == 1


def test_releasing_a_slider_sends_one_move_and_reads_back_once(zoom, parent, run_loop, caplog):
    caplog.set_level(logging.INFO, logger="reolink_viewer.zoom")
    with FakeCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        press(window.zoom)
        for position in (14, 19, 25):
            window.zoom.scale.set_value(position)
        pause(run_loop, zoom)
        assert fake.moves == []  # nothing is sent while the slider is held
        assert window.zoom.number.get_text() == "25"

        release(window.zoom)
        assert window.status.get_text() == zoom.MOVING
        assert run_loop(lambda: fake.moves)
        fake.focus = 87  # the camera refocuses by itself after the zoom move
        assert run_loop(lambda: window.status.get_text() == zoom.DONE)
        assert (window.zoom.position, window.focus.position) == (25, 87)
        assert window.focus.number.get_text() == "87"
        pause(run_loop, zoom, 8)
        assert fake.moves == [("ZoomPos", 25)]  # showing the new focus position asked for no move
        assert fake.commands.count("GetZoomFocus") == 2  # on opening, and once after the move
        assert any("Garage" in line and "zoom" in line and "25" in line for line in caplog.messages)


def test_a_lens_still_on_its_way_is_not_called_done(zoom, parent, run_loop):
    with FakeCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        window.zoom.scale.set_value(30)
        assert run_loop(lambda: fake.moves)
        fake.zoom = 21  # a long move: the read after the wait catches the lens half way
        assert run_loop(lambda: window.status.get_text() == zoom.NOT_THERE_YET)
        assert window.zoom.position == 21
        fake.zoom = 30
        window.refresh_button.clicked()
        assert run_loop(lambda: window.zoom.position == 30)
        pause(run_loop, zoom)
        assert window.status.get_text() == "" and fake.moves == [("ZoomPos", 30)]


def test_a_held_slider_is_not_pulled_away_by_the_read_back(zoom, parent, run_loop):
    with FakeCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        window.zoom.scale.set_value(20)
        assert run_loop(lambda: fake.moves)
        fake.focus = 87
        press(window.focus)  # the user grabs the focus slider while the lens is still settling
        assert run_loop(lambda: window.status.get_text() == zoom.DONE)
        assert window.focus.position == 120
        release(window.focus)  # let go without moving it: now it shows where the camera is
        assert window.focus.position == 87 and window.focus.number.get_text() == "87"
        pause(run_loop, zoom)
        assert fake.moves == [("ZoomPos", 20)]


def test_a_drag_cut_short_by_another_window_still_sends_its_position(zoom, parent, run_loop):
    from gi.repository import Gdk

    with FakeCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        press(window.zoom)
        window.zoom.scale.set_value(15)
        broken = Gdk.Event.new(Gdk.EventType.GRAB_BROKEN)
        seat = Gdk.Display.get_default().get_default_seat()
        if seat is not None:  # GTK asks the event for its pointer
            broken.set_device(seat.get_pointer())
        window.zoom.scale.emit("grab-broken-event", broken)
        assert run_loop(lambda: window.status.get_text() == zoom.DONE)
        assert fake.moves == [("ZoomPos", 15)]


def test_a_slider_pressed_but_not_moved_sends_nothing(zoom, parent, run_loop):
    with FakeCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        press(window.focus)
        release(window.focus)
        pause(run_loop, zoom)
        assert fake.moves == [] and window.status.get_text() == ""


def test_quick_changes_send_only_the_last_position(zoom, parent, run_loop):
    with FakeCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        for position in (11, 12, 13, 17):  # arrow keys or the scroll wheel
            window.zoom.scale.set_value(position)
        assert run_loop(lambda: window.status.get_text() == zoom.DONE)
        assert fake.moves == [("ZoomPos", 17)]

        for _ in range(3):
            window.focus.plus.clicked()
        window.focus.minus.clicked()
        assert window.focus.number.get_text() == "122"
        assert run_loop(lambda: len(fake.moves) == 2)
        assert run_loop(lambda: window.status.get_text() == zoom.DONE)
        pause(run_loop, zoom)
        assert fake.moves == [("ZoomPos", 17), ("FocusPos", 122)]


def test_changes_during_a_move_are_coalesced_into_one_more_move(zoom, parent, run_loop):
    with HeldCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        window.zoom.scale.set_value(20)
        assert run_loop(fake.move_arrived.is_set)
        for position in (22, 24, 30):  # each one settles while the first move is still in flight
            window.zoom.scale.set_value(position)
            assert run_loop(lambda: window.zoom.target == position)
        assert fake.moves == []
        fake.let_go.set()
        assert run_loop(lambda: window.status.get_text() == zoom.DONE)
        pause(run_loop, zoom)
        assert fake.moves == [("ZoomPos", 20), ("ZoomPos", 30)]
        assert window.zoom.position == 30
        assert fake.commands.count("GetZoomFocus") == 2  # the first move's read-back was cancelled


def test_autofocus_box_switches_the_camera(zoom, parent, run_loop):
    with FakeCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        window.autofocus.set_active(False)
        assert window.autofocus_note.get_text() == zoom.AUTOFOCUS_OFF
        assert run_loop(lambda: fake.autofocus_disabled == 1)
        assert run_loop(lambda: window.status.get_text() == zoom.DONE)
        assert not window.autofocus.get_active()
        window.autofocus.set_active(True)
        assert run_loop(lambda: window.status.get_text() == zoom.DONE)
        pause(run_loop, zoom)
        assert fake.autofocus_disabled == 0 and window.autofocus.get_active()
        assert fake.commands.count("SetAutoFocus") == 2  # reading the state back didn't switch it again
        assert fake.moves == []


def test_no_autofocus_box_for_a_camera_without_the_switch(zoom, parent, run_loop):
    with FakeCamera(autofocus=False) as fake:
        window = open_window(zoom, parent, fake, run_loop)
        assert window.controls.get_sensitive() and window.zoom.position == 10
        assert not window.autofocus.get_visible() and not window.autofocus_note.get_visible()


def test_camera_without_motorised_zoom_shows_the_sentence(zoom, parent, run_loop):
    with FakeCamera(zoom=False) as fake:
        window = open_window(zoom, parent, fake, run_loop)
        assert window.no_lens.is_visible()
        assert window.no_lens.get_text() == "This camera has no motorised zoom or focus."
        assert not window.controls.is_visible() and not window.refresh_button.is_visible()
        assert fake.moves == []


def test_rejected_login_is_shown_and_never_tried_again(zoom, parent, run_loop):
    with FakeCamera(password="right") as fake:
        window = open_window(zoom, parent, fake, run_loop, password="wrong")
        assert "rejected the user name or password" in window.status.get_text()
        assert not window.controls.get_sensitive()
        for _ in range(2):
            window.refresh_button.clicked()
            assert window.status.get_text() == zoom.READING
            assert run_loop(lambda: "rejected" in window.status.get_text())
        pause(run_loop, zoom)
        assert fake.login_attempts == 1
        assert fake.commands == ["Login"]


def test_a_login_lost_later_is_tried_once_and_drops_what_was_waiting(zoom, parent, run_loop, caplog):
    caplog.set_level(logging.INFO, logger="reolink_viewer.zoom")
    with FakeCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        fake.tokens.clear()  # the camera restarted, and someone changed its password meanwhile
        fake.password = "changed"
        for position in (20, 21):
            caplog.clear()
            window.status.set_text("")
            window.zoom.scale.set_value(position)
            window.focus.scale.set_value(50 + position)
            assert run_loop(lambda: "rejected" in window.status.get_text())
            pause(run_loop, zoom, 6)
            assert fake.login_attempts == 2  # on opening, and one more when the session was gone
            assert fake.moves == []
            # The focus move that was waiting behind the failed zoom move was dropped, not tried too.
            assert [line for line in caplog.messages if "moving the" in line] == [
                f"[Garage] moving the zoom to {position}"]


def test_closing_with_a_pending_move_sends_nothing(zoom, parent, run_loop):
    with FakeCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        cam_id = window.cam.id
        window.zoom.scale.set_value(20)  # would be sent once it settles
        window.destroy()
        assert cam_id not in zoom._windows
        pause(run_loop, zoom, 6)
        assert fake.moves == []
        assert fake.commands.count("GetZoomFocus") == 1


def test_closing_during_a_move_ignores_its_answer_and_what_was_waiting(zoom, parent, run_loop):
    with HeldCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        window.zoom.scale.set_value(20)
        assert run_loop(fake.move_arrived.is_set)
        window.focus.scale.set_value(50)
        assert run_loop(lambda: window.focus.target == 50)
        window.destroy()
        fake.let_go.set()
        assert run_loop(lambda: fake.moves)
        pause(run_loop, zoom, 8)
        assert fake.moves == [("ZoomPos", 20)]  # the one already on its way; the focus move never went out
        assert fake.commands.count("GetZoomFocus") == 1  # and no read-back for a closed window


def test_one_window_per_camera(zoom, parent, run_loop):
    with FakeCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        zoom.show_zoom_focus(parent, window.cam)
        assert list(zoom._windows.values()) == [window]
        pause(run_loop, zoom)
        assert fake.commands.count("GetZoomFocus") == 1  # presenting the open window read nothing

        renamed = dataclasses.replace(window.cam, name="Garage South")
        zoom.show_zoom_focus(parent, renamed)  # an edited camera gets a fresh window
        fresh = zoom._windows[renamed.id]
        assert fresh is not window and fresh.cam == renamed and "Garage South" in fresh.get_title()
        assert run_loop(fresh.controls.get_sensitive)
        assert fake.login_attempts == 1  # the same login: the session is shared

        zoom.close_zoom_focus(renamed.id)
        zoom.close_zoom_focus(renamed.id)  # already closed: nothing to do
        assert zoom._windows == {}


def test_does_not_open_when_the_login_is_not_allowed(zoom, parent, run_loop, monkeypatch):
    from reolink_viewer import apiui

    asked = []
    monkeypatch.setattr(apiui, "ensure_login_allowed", lambda window, cam: asked.append(cam) and False)
    with FakeCamera() as fake:
        cam = camera(fake)
        zoom.show_zoom_focus(parent, cam)
        pause(run_loop, zoom)
        assert asked == [cam] and zoom._windows == {}
        assert fake.commands == []


class RefusingCamera(FakeCamera):
    """A camera that answers a change with an error code: nothing changes in it."""

    def _answer(self, cmd: dict, token: str) -> dict:
        if cmd.get("cmd") in ("SetAutoFocus", "StartZoomFocus") and token in self.tokens:
            self.commands.append(cmd["cmd"])
            return {"cmd": cmd["cmd"], "code": 0, "value": {"rspCode": -1}}
        return super()._answer(cmd, token)


def test_a_refused_change_puts_the_controls_back(zoom, parent, run_loop):
    with RefusingCamera() as fake:
        window = open_window(zoom, parent, fake, run_loop)
        assert window.autofocus.get_active()
        window.autofocus.set_active(False)
        assert run_loop(lambda: "didn't accept" in window.status.get_text())
        assert window.autofocus.get_active() and fake.autofocus_disabled == 0
        assert window.autofocus_note.get_text() == zoom.AUTOFOCUS_ON

        window.zoom.scale.set_value(20)
        assert run_loop(lambda: "StartZoomFocus" in fake.commands)
        assert run_loop(lambda: window.zoom.position == 10)  # where the camera says it is
        assert window.zoom.number.get_text() == "10"


def test_a_locked_login_can_be_tried_again_from_the_open_window(zoom, parent, run_loop, monkeypatch):
    from reolink_viewer import apiui

    asked = []
    monkeypatch.setattr(apiui, "_ask", lambda window, question, detail, go: asked.append(question) or True)
    with FakeCamera(lock_after=0) as fake:
        window = open_window(zoom, parent, fake, run_loop)
        assert "locked" in window.status.get_text()
        fake.lock_after = None  # the lock has cleared
        zoom.show_zoom_focus(parent, window.cam)  # what the message says to do
        assert len(asked) == 1 and zoom._windows[window.cam.id] is window
        assert run_loop(window.controls.get_sensitive)
        assert fake.login_attempts == 2
        zoom.show_zoom_focus(parent, window.cam)  # no lock any more: just brought to the front
        pause(run_loop, zoom)
        assert len(asked) == 1 and fake.login_attempts == 2
