"""The camera tile against a fake RTSP camera (needs a display and GStreamer's RTSP server bindings)."""

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reolink_viewer.config import Camera

FAKE_CAMERA = Path(__file__).with_name("fake_rtsp_camera.py")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def camera_port(gtk):
    try:
        import gi

        gi.require_version("GstRtspServer", "1.0")  # what the fake camera needs
        from gi.repository import Gst
    except (ImportError, ValueError):
        pytest.skip("GStreamer RTSP server bindings (gir1.2-gst-rtsp-server-1.0) are not installed")
    if Gst.ElementFactory.find("x264enc") is None:
        pytest.skip("x264enc is not installed")
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(FAKE_CAMERA), str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    assert "ready" in proc.stdout.readline()
    yield port
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def make_tile(gtk, camera_port, monkeypatch):
    from reolink_viewer import player

    monkeypatch.setattr(player, "SINK_MODE", "cpu")  # no GPU needed
    player.configure(False)
    made = []

    def make(password="p@ss", **kwargs):
        cam = Camera(name="Test", host="127.0.0.1", port=camera_port, password=password, **kwargs)
        tile = player.CameraTile(cam)
        window = gtk.Window()
        window.set_default_size(320, 200)
        window.add(tile)
        window.show_all()
        made.append((tile, window))
        return tile

    yield make
    for tile, window in made:
        tile.dispose()
        window.destroy()


def connects(caplog) -> int:
    return sum("connecting to" in record.getMessage() for record in caplog.records)


def test_tile_goes_live(make_tile, run_loop):
    tile = make_tile()
    tile.start()
    assert run_loop(lambda: tile._status == "Live", 15)


def test_rejected_login_is_tried_once(make_tile, run_loop, caplog):
    from reolink_viewer import player

    tile = make_tile(password="wrong")
    with caplog.at_level(logging.INFO, logger="reolink_viewer.player"):
        tile.start()
        assert run_loop(lambda: tile._status == player.LOGIN_REJECTED, 15)
        run_loop(lambda: False, 4)  # the old code retried after 2 s, 4 s, 8 s…
        assert connects(caplog) == 1 and not tile.is_playing
        tile.stop()  # what maximizing another camera does to this tile…
        tile.start()  # …and going back to the grid
        run_loop(lambda: False, 1)
        assert connects(caplog) == 1
        assert tile._status == player.LOGIN_REJECTED  # still says why it's dark
        tile.reconnect()  # the user asks: one more try
        assert run_loop(lambda: connects(caplog) == 2, 5)
        assert run_loop(lambda: tile._status == player.LOGIN_REJECTED, 15)


def test_editing_the_password_tries_again(make_tile, run_loop):
    import dataclasses

    from reolink_viewer import player

    tile = make_tile(password="wrong")
    tile.start()
    assert run_loop(lambda: tile._status == player.LOGIN_REJECTED, 15)
    tile.set_camera(dataclasses.replace(tile.camera, password="p@ss"))
    assert run_loop(lambda: tile._status == "Live", 15)


def test_renaming_does_not_reconnect(make_tile, run_loop, caplog):
    import dataclasses

    tile = make_tile()
    tile.start()
    assert run_loop(lambda: tile._status == "Live", 15)
    with caplog.at_level(logging.INFO, logger="reolink_viewer.player"):
        tile.set_camera(dataclasses.replace(tile.camera, name="Renamed"))
        run_loop(lambda: False, 0.5)
    assert connects(caplog) == 0 and tile.is_playing
    assert "Renamed" in tile._label.get_text()


def test_snapshot_does_not_block_and_returns_a_png(make_tile, run_loop):
    tile = make_tile(stream="main")
    tile.start()
    assert run_loop(lambda: tile._status == "Live", 15)
    got = []
    began = time.monotonic()
    tile.snapshot_png(got.append)
    assert time.monotonic() - began < 0.1  # the conversion happens elsewhere
    assert run_loop(lambda: bool(got), 10)
    assert got[0].startswith(b"\x89PNG")


def test_snapshot_without_a_picture_says_so(make_tile):
    got = []
    make_tile().snapshot_png(got.append)
    assert got == [None]


def test_caption_shows_a_passing_notice(make_tile, run_loop):
    tile = make_tile()
    tile.flash("Snapshot saved", seconds=1)
    assert "Snapshot saved" in tile._label.get_text()
    assert run_loop(lambda: "Snapshot saved" not in tile._label.get_text(), 3)


class _Info:
    def __init__(self, pts):
        self._pts = pts

    def get_buffer(self):
        return self

    @property
    def pts(self):
        return self._pts


def test_pacer_shows_the_first_frame_at_once_then_paces(gtk):
    from gi.repository import Gst

    from reolink_viewer.player import FramePacer

    pacer = FramePacer(Gst.ElementFactory.make("queue", None))
    pacer._gap_peak = 0.5  # a bursty camera: cushion 0.6 s
    for attempt in range(2):  # a new connection behaves the same after reset()
        began = time.monotonic()
        pacer._on_arrive(None, _Info(1))
        assert pacer._on_release(None, _Info(1)) == Gst.PadProbeReturn.OK
        assert time.monotonic() - began < 0.05  # not held for the cushion
        began = time.monotonic()
        pacer._on_arrive(None, _Info(2))
        pacer._on_release(None, _Info(2))
        assert time.monotonic() - began > 0.4  # the second frame opens the schedule
        pacer.reset()


def test_pacer_forgets_the_frame_rate_for_a_new_stream(gtk):
    from gi.repository import Gst

    from reolink_viewer.player import FramePacer

    pacer = FramePacer(Gst.ElementFactory.make("queue", None))
    pacer._interval, pacer._gap_peak = 0.04, 0.3
    pacer.reset()
    assert pacer._interval == 0.04
    pacer.reset(new_stream=True)
    assert pacer._interval is None and pacer._gap_peak == 0.3  # the cushion is the camera's
