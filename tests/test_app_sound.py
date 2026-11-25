"""Which camera plays sound: the main window's rules, with the tiles' connections stubbed out.

The grid is silent; a maximized camera plays its sound (for a dual-lens camera
only the lens the user picked); nothing live plays while Recordings is open.
"""

import pytest

from reolink_viewer.config import Camera, Settings


@pytest.fixture
def window(gtk, monkeypatch, tmp_path):
    from gi.repository import Gio

    from reolink_viewer import app as app_module, player

    # The tiles don't connect anywhere: only what they're told is recorded.
    def start(tile):
        tile._wanted = True

    def stop(tile, status=None):
        tile._wanted = False

    monkeypatch.setattr(player.CameraTile, "start", start)
    monkeypatch.setattr(player.CameraTile, "stop", stop)
    monkeypatch.setattr(player.CameraTile, "restart", start)
    monkeypatch.setattr(app_module.Neolink, "apply", lambda self, cameras: None)
    monkeypatch.setattr(app_module.Neolink, "stop", lambda self: None)

    viewer = app_module.ViewerApp(tmp_path / "config.json", [])
    viewer.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    viewer.register(None)
    viewer.settings = Settings(cameras=[
        Camera(name="Garage East", host="192.0.2.1", password="pw", id="east"),
        Camera(name="Garage South", host="192.0.2.2", password="pw", id="south"),
        Camera(name="Driveway", protocol="battery", uid="TESTUID000000000", channel=1, id="lens1"),
        Camera(name="Driveway 2", protocol="battery", uid="TESTUID000000000", channel=2, id="lens2"),
    ])
    main = app_module.MainWindow(viewer)
    yield main
    main.shut_down()
    main.destroy()


def audible(window) -> list[str]:
    return [tile.camera.name for tile in window.ordered_tiles() if tile._audible()]


def test_the_grid_is_silent_and_the_picked_camera_plays(window):
    assert audible(window) == []
    window.maximize(window.tiles["south"])
    assert audible(window) == ["Garage South"]
    window.restore()
    assert audible(window) == []


def test_only_the_picked_lens_of_a_dual_lens_camera_plays(window):
    window.maximize(window.tiles["lens2"])
    assert window.shown == [window.tiles["lens1"], window.tiles["lens2"]]
    assert audible(window) == ["Driveway 2"]


def test_a_camera_whose_sound_is_off_stays_silent(window):
    import dataclasses

    tile = window.tiles["east"]
    tile.set_camera(dataclasses.replace(tile.camera, sound=False))
    window.maximize(tile)
    assert audible(window) == []


def test_nothing_live_plays_while_recordings_are_open(window, monkeypatch):
    from reolink_viewer import recordings

    monkeypatch.setattr(recordings, "is_open", lambda: True)
    window.maximize(window.tiles["east"])
    assert audible(window) == []
    monkeypatch.setattr(recordings, "is_open", lambda: False)
    window._update_sound()  # the Recordings window was closed
    assert audible(window) == ["Garage East"]


def test_opening_recordings_silences_live_sound_until_it_is_closed(gtk, window, monkeypatch):
    from reolink_viewer import recordings

    def show(parent, cam):  # a stand-in window: no camera API here
        if recordings._window is None:
            recordings._window = gtk.Window(title="Recordings")
        recordings._window.show()

    monkeypatch.setattr(recordings, "show_recordings", show)
    monkeypatch.setattr(recordings, "_window", None)
    window.maximize(window.tiles["east"])
    assert audible(window) == ["Garage East"]
    window.show_recordings(window.tiles["east"])
    assert window.focused is None and audible(window) == []  # back to the grid, and silent
    window.maximize(window.tiles["south"])
    assert audible(window) == []  # the recording has the speakers
    recordings._window.hide()
    assert audible(window) == ["Garage South"]
    recordings._window.destroy()
    monkeypatch.setattr(recordings, "_window", None)  # the window's own shutdown isn't there
