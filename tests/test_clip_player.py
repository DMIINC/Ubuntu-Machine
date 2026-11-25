"""ClipPlayer in a real window, playing a short H.264 clip made on the spot.

Needs a display (see the "gtk" fixture) and the x264enc and mp4mux elements.
Everything is imported inside fixtures: Gtk must not be loaded before
prepare_display() has run.

Under headless weston with REOLINK_VIEWER_SINK=wayland the pause and step
tests fail: that compositor sends hardly any frame callbacks, and
gtkwaylandsink then drops frames instead of holding one, also while paused.
"""

import logging
import socket
import time
from datetime import date, datetime

import pytest

from reolink_viewer.api import CameraApi
from reolink_viewer.config import Camera

from fake_reolink_api import FakeCamera, clip as recording

SECONDS = 4
FRAME = 1 / 25


@pytest.fixture(scope="module")
def clip_player(gtk):
    from reolink_viewer import clip_player

    return clip_player


@pytest.fixture(scope="module")
def clip(gtk, tmp_path_factory):
    """A 4 s, 25 fps H.264 MP4 with a key frame every second (index at the end, as cameras write it)."""
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
    return path


class Stage:
    """A window with a ClipPlayer in it, and a record of what the player reported."""

    def __init__(self, gtk, clip_player):
        self.window = gtk.Window(title="Recording test")
        self.window.set_default_size(640, 480)
        self.player = clip_player.ClipPlayer()
        self.window.add(self.player)
        self.window.show_all()
        self.states: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.ended = 0
        self.player.on_state = self.states.append
        self.player.on_error = lambda kind, text: self.errors.append((kind, text))
        self.player.on_ended = self._on_ended

    def _on_ended(self) -> None:
        self.ended += 1

    def forget(self) -> None:
        self.player.stop()
        self.states.clear()
        self.errors.clear()
        self.ended = 0

    def close(self) -> None:
        self.player.shutdown()
        self.window.destroy()


@pytest.fixture(scope="module")
def shared_stage(gtk, clip_player):
    stage = Stage(gtk, clip_player)
    yield stage
    stage.close()


@pytest.fixture
def stage(shared_stage):
    """One player for most tests, as in the app: every clip reuses its playbin and sink."""
    shared_stage.forget()
    return shared_stage


@pytest.fixture
def own_stage(gtk, clip_player):
    """A player of its own, for tests that change or end it."""
    stage = Stage(gtk, clip_player)
    yield stage
    stage.close()


def start(stage: Stage, run_loop, uri: str, **options) -> None:
    stage.player.open(uri, **options)
    assert run_loop(lambda: stage.player.state in ("playing", "error")), stage.player.state
    assert stage.player.state == "playing", stage.errors


def at(stage: Stage, run_loop, seconds: float, tolerance: float = FRAME / 2) -> bool:
    return run_loop(lambda: abs((stage.player.position or -9) - seconds) <= tolerance, 5)


def camera_url(fake: FakeCamera) -> tuple[str, float]:
    """A recording's playback URL and length, got the way the app gets them."""
    fake.add_clips(recording(datetime(2026, 11, 24, 8), SECONDS, "sub"))
    session = CameraApi(Camera(name="Test", host="127.0.0.1", password="pw"), ports=((fake.port, False),))
    found = session.recordings(date(2026, 11, 24), "sub")[0]
    return session.playback_url(found), found.duration.total_seconds()


def test_opens_and_knows_the_length(stage, clip, run_loop):
    assert stage.player.state == "idle" and stage.player.position is None
    start(stage, run_loop, clip.as_uri())
    assert stage.states == ["opening", "playing"]
    assert stage.player.duration == pytest.approx(SECONDS, abs=0.1)
    assert stage.player.seekable and stage.player.output in ("gl", "wayland", "cpu")


def test_play_advances_at_clock_speed_and_pause_holds(stage, clip, run_loop):
    start(stage, run_loop, clip.as_uri())
    run_loop(lambda: False, 0.3)
    before, started = stage.player.position, time.monotonic()
    run_loop(lambda: False, 1.0)
    moved, elapsed = stage.player.position - before, time.monotonic() - started
    assert moved == pytest.approx(elapsed, abs=0.15)  # by time stamps, not as fast as it decodes

    stage.player.pause()
    run_loop(lambda: False, 0.3)
    held = stage.player.position
    run_loop(lambda: False, 0.5)
    assert stage.player.state == "paused" and stage.player.position == pytest.approx(held, abs=0.001)

    stage.player.toggle()
    assert stage.player.state == "playing"
    assert run_loop(lambda: stage.player.position > held + 0.2, 3)


def test_seek(stage, clip, run_loop):
    start(stage, run_loop, clip.as_uri())
    stage.player.pause()
    stage.player.seek(2.6)
    assert at(stage, run_loop, 2.6), stage.player.position
    stage.player.seek(1.7, accurate=False)  # settles for the key frame before
    assert at(stage, run_loop, 1.0), stage.player.position
    stage.player.seek(99)
    assert run_loop(lambda: (stage.player.position or 0) > SECONDS - 0.2, 5)
    assert stage.player.position <= stage.player.duration and not stage.errors


def test_of_many_seeks_the_newest_wins(stage, clip, run_loop):
    start(stage, run_loop, clip.as_uri())
    stage.player.pause()
    for target in (0.5, 3.0, 1.2, 2.2, 0.8, 3.4, 1.6):  # as a drag sends them, faster than they finish
        stage.player.seek(target, accurate=False)
    stage.player.seek(2.84)
    assert at(stage, run_loop, 2.84), stage.player.position
    run_loop(lambda: False, 0.3)
    assert stage.player.position == pytest.approx(2.84, abs=FRAME / 2)


def test_skips_add_up(stage, clip, run_loop):
    start(stage, run_loop, clip.as_uri())
    stage.player.pause()
    stage.player.seek(0.4)
    for _ in range(3):  # as arrow keys send them, faster than a jump finishes
        stage.player.skip(1.0)
    assert at(stage, run_loop, 3.4), stage.player.position
    run_loop(lambda: False, 0.3)
    stage.player.skip(-1.0)
    stage.player.skip(-1.0)
    assert at(stage, run_loop, 1.4), stage.player.position
    stage.player.skip(-9)  # no further back than the start
    assert run_loop(lambda: stage.player.position == 0.0, 5), stage.player.position
    stage.player.skip(99)  # no further than the last picture
    assert run_loop(lambda: (stage.player.position or 0) > SECONDS - 0.2, 5) and not stage.errors


def test_step_shows_the_next_frame(stage, clip, run_loop):
    start(stage, run_loop, clip.as_uri())
    stage.player.pause()
    stage.player.seek(1.0)
    assert at(stage, run_loop, 1.0)
    run_loop(lambda: False, 0.3)  # a step asked for while the jump is under way is ignored
    stage.player.step()
    assert at(stage, run_loop, 1.0 + FRAME, 0.005), stage.player.position
    assert stage.player.state == "paused"


def test_speed(stage, clip, run_loop):
    start(stage, run_loop, clip.as_uri())
    stage.player.set_speed(2.0)
    run_loop(lambda: False, 0.4)
    before, started = stage.player.position, time.monotonic()
    run_loop(lambda: False, 0.8)
    rate = (stage.player.position - before) / (time.monotonic() - started)
    assert stage.player.speed == 2.0 and rate == pytest.approx(2.0, abs=0.3)
    start(stage, run_loop, clip.as_uri())
    assert stage.player.speed == 1.0  # every clip starts at normal speed


def test_end_of_clip(stage, clip, run_loop):
    start(stage, run_loop, clip.as_uri())
    stage.player.seek(SECONDS - 0.5)
    assert run_loop(lambda: stage.player.state == "ended", 5)
    assert stage.ended == 1 and stage.states[-1] == "ended"
    assert stage.player.position == pytest.approx(SECONDS, abs=0.1)
    stage.player.play()  # from the start again
    assert stage.player.state == "playing"
    assert run_loop(lambda: 0.2 < (stage.player.position or 9) < 1.5, 5)
    assert stage.ended == 1 and not stage.errors


def test_plays_a_cameras_playback_url(stage, clip, run_loop, caplog):
    with FakeCamera(clip_bytes=clip.read_bytes()) as fake, caplog.at_level(logging.DEBUG):
        url, length = camera_url(fake)
        start(stage, run_loop, url, duration_hint_seconds=length)
        assert stage.player.seekable and stage.player.duration == pytest.approx(SECONDS, abs=0.1)
        stage.player.pause()
        stage.player.seek(3.0)  # the index sits at the file's end: this needs Range requests
        assert at(stage, run_loop, 3.0), stage.player.position
        assert not stage.errors
        assert sum("cmd=Playback" in path for path in fake.paths) > 1
    assert "token=***" in caplog.text and url.rsplit("token=", 1)[1] not in caplog.text


def test_a_camera_without_range_calls_for_a_download(stage, clip, clip_player, run_loop, caplog):
    with FakeCamera(clip_bytes=clip.read_bytes(), ranges=False) as fake, caplog.at_level(logging.DEBUG):
        url, length = camera_url(fake)
        stage.player.open(url, length)
        assert run_loop(lambda: stage.player.state in ("playing", "error"))
        stage.player.seek(3.0)  # a clip this small does start; jumping in it is what fails
        assert run_loop(lambda: stage.player.state == "error")
    assert stage.errors[-1][0] in clip_player.NEEDS_DOWNLOAD
    assert stage.player.position is None and stage.states[-1] == "error"
    # GStreamer's error text quotes the URL: the token must not reach the log.
    assert "token=***" in caplog.text and url.rsplit("token=", 1)[1] not in caplog.text


def test_a_cameras_refusal_is_reported(stage, clip, run_loop):
    with FakeCamera(clip_bytes=clip.read_bytes()) as fake:
        url, _ = camera_url(fake)
        stage.player.open(url.rsplit("token=", 1)[0] + "token=expired")
        assert run_loop(lambda: stage.player.state == "error")
    assert [kind for kind, _ in stage.errors] == ["not-video"]
    assert "login" in stage.player.status


def test_bad_and_missing_files_are_reported_and_the_next_clip_plays(stage, clip, tmp_path, run_loop):
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(bytes(range(256)) * 80)
    stage.player.open(garbage.as_uri())
    assert run_loop(lambda: stage.player.state == "error")
    stage.player.open((tmp_path / "missing.mp4").as_uri())
    assert run_loop(lambda: len(stage.errors) == 2)
    assert [kind for kind, _ in stage.errors] == ["not-video", "not-found"]
    assert stage.player.status == stage.errors[-1][1]
    stage.player.play()  # nothing to play: stays as it is, and tries nothing by itself
    assert stage.player.state == "error"
    start(stage, run_loop, clip.as_uri())


def test_unreachable_camera(stage, run_loop):
    with FakeCamera() as fake:
        port = fake.port
    stage.player.open(f"http://127.0.0.1:{port}/cgi-bin/api.cgi?cmd=Playback&source=x&token=t")
    assert run_loop(lambda: stage.player.state == "error")
    assert [kind for kind, _ in stage.errors] == ["network"]


def test_a_camera_that_goes_quiet_is_noticed(stage, clip, clip_player, run_loop, monkeypatch):
    monkeypatch.setattr(clip_player, "STALL_TIMEOUT_S", 1)
    # Every answer stops after its first 16 KB: enough for a picture or two, then silence.
    with FakeCamera(clip_bytes=clip.read_bytes(), rate=1) as fake:
        url, length = camera_url(fake)
        stage.player.open(url, length)
        assert run_loop(lambda: stage.player.state == "error", 10)
    assert stage.errors == [("network", "The camera stopped sending the recording")]


def test_a_recording_that_never_starts_is_given_up(stage, clip_player, run_loop, monkeypatch):
    monkeypatch.setattr(clip_player, "OPEN_TIMEOUT_S", 1)
    with socket.socket() as silent:  # takes the connection and never answers
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        stage.player.open(f"http://127.0.0.1:{silent.getsockname()[1]}/clip.mp4")
        assert run_loop(lambda: stage.player.state == "error", 10)
    assert stage.errors == [("network", "The recording did not start")]


def test_missing_decoder(own_stage, clip, run_loop):
    from gi.repository import Gst

    decoders = [factory for factory in Gst.Registry.get().get_feature_list(Gst.ElementFactory)
                if "Decoder/Video" in (factory.get_metadata("klass") or "")]
    ranks = [factory.get_rank() for factory in decoders]
    for factory in decoders:
        factory.set_rank(Gst.Rank.NONE)  # what GPU-only mode does to software decoders
    try:
        own_stage.player.open(clip.as_uri())
        assert run_loop(lambda: own_stage.player.state == "error")
    finally:
        for factory, rank in zip(decoders, ranks):
            factory.set_rank(rank)
    assert [kind for kind, _ in own_stage.errors] == ["no-decoder"]


def test_a_failing_gpu_output_falls_back_like_the_live_player(
        gtk, own_stage, clip, clip_player, run_loop, monkeypatch):
    from gi.repository import GLib, Gst

    from reolink_viewer import player

    monkeypatch.setattr(player, "_failed_kinds", set())
    if own_stage.player.output != "gl":
        pytest.skip("the GPU output isn't in use here")
    start(own_stage, run_loop, clip.as_uri())
    first_widget = own_stage.player._video
    error = GLib.Error.new_literal(Gst.ResourceError.quark(), "no GL here", Gst.ResourceError.FAILED)
    own_stage.player._playbin.get_bus().post(
        Gst.Message.new_error(own_stage.player._gtk_sink, error, "made up for the test"))
    assert run_loop(lambda: own_stage.player.output != "gl" and own_stage.player.state == "playing")
    second = own_stage.player.output  # wayland in a Wayland session, else cpu
    assert second in ("wayland", "cpu") and player._failed_kinds == {"gl"} and not own_stage.errors
    assert not first_widget.get_visible() and own_stage.player._video.get_visible()
    assert run_loop(lambda: (own_stage.player.position or 0) > 0.5, 5)  # the clip plays on

    # The next player skips the output that failed. In GPU-only mode, once the
    # other GPU output has failed too, nothing is left.
    other = Stage(gtk, clip_player)
    try:
        assert other.player.output == second
        player._failed_kinds.add("wayland")
        monkeypatch.setattr(player, "GPU_ONLY", True)
        strict = Stage(gtk, clip_player)
        try:
            assert strict.player.output == ""
            strict.player.open(clip.as_uri())
            assert strict.player.state == "error" and strict.errors[0][0] == "other"
            assert "GPU" in strict.errors[0][1]
        finally:
            strict.close()
    finally:
        other.close()


def test_the_wayland_output_is_renewed_after_its_window_was_hidden(own_stage, clip, run_loop):
    if own_stage.player.output != "wayland":
        pytest.skip("needs a Wayland session and REOLINK_VIEWER_SINK=wayland")
    start(own_stage, run_loop, clip.as_uri())
    first = own_stage.player._video
    own_stage.player.stop()
    own_stage.window.hide()  # gives up the surface gtkwaylandsink drew on
    own_stage.window.show()
    start(own_stage, run_loop, clip.as_uri())
    assert own_stage.player.output == "wayland" and own_stage.player._video is not first
    assert own_stage.player._video.get_visible() and not first.get_visible()
    second = own_stage.player._video
    start(own_stage, run_loop, clip.as_uri())  # the window stayed: so does the output
    assert own_stage.player._video is second and not own_stage.errors


def test_stop_shutdown_and_sinks_that_outlive_the_player(own_stage, clip, clip_player, run_loop):
    start(own_stage, run_loop, clip.as_uri())
    sink = own_stage.player._gtk_sink
    own_stage.player.stop()
    assert own_stage.player.state == "idle" and own_stage.player.position is None
    assert own_stage.states[-1] == "idle"
    start(own_stage, run_loop, clip.as_uri())
    own_stage.player.shutdown()
    own_stage.player.shutdown()
    own_stage.player.open(clip.as_uri())  # ignored from now on
    assert own_stage.player.state == "idle"
    # Freeing a gtkglsink breaks GTK's GL on Wayland: every sink is kept for good.
    assert any(kept is sink for _, kept in clip_player._sinks)


def test_open_outside_a_window_is_refused(clip_player, clip):
    lonely = clip_player.ClipPlayer()
    with pytest.raises(RuntimeError):
        lonely.open(clip.as_uri())  # the GTK sink would put the player in a window of its own
    lonely.shutdown()
