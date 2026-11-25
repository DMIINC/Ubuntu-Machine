import tomllib

from reolink_viewer.config import NEOLINK_PORT, Camera
from reolink_viewer.neolink import config_text


def test_config_serves_awake_battery_cameras_locally():
    lens1 = Camera(name="Driveway", protocol="battery", uid=" ABCDEF0123456789 ",
                   password='p"a\\ss!word', id="abc123")
    lens2 = Camera(name="Driveway 2", protocol="battery", uid="ABCDEF0123456789",
                   host=" 192.168.1.35 ", channel=2, id="def456")
    conf = tomllib.loads(config_text([(lens1, "main"), (lens2, "sub")]))
    assert conf["bind"] == "127.0.0.1"
    assert conf["bind_port"] == NEOLINK_PORT
    first, second = conf["cameras"]
    assert first["name"] == "abc123"  # the RTSP path, unlike the name never changes
    assert first["uid"] == "ABCDEF0123456789"
    assert first["password"] == 'p"a\\ss!word'
    assert first["discovery"] == "local"  # never through Reolink's servers
    assert first["push_notifications"] is False
    assert first["use_splash"] is False
    # Only the stream being watched; the second lens is channel 1 for neolink.
    assert (first["stream"], first["channel_id"]) == ("mainStream", 0)
    assert (second["stream"], second["channel_id"]) == ("subStream", 1)
    # neolink runs only while watched, so its own pause / idle handling is off.
    assert "pause" not in first and "idle_disconnect" not in first
    # With an IP, wake-ups also go straight to the camera, not only by broadcast.
    assert "address" not in first
    assert second["address"] == "192.168.1.35"


# --- the manager's bookkeeping (no process is started in these tests) ---

import logging  # noqa: E402

from reolink_viewer import neolink as neolink_module  # noqa: E402
from reolink_viewer.neolink import Neolink, _environment, find_shim  # noqa: E402

LENS_1 = Camera(name="Driveway", protocol="battery", uid="ABCDEF0123456789", id="lens1")
LENS_2 = Camera(name="Driveway 2", protocol="battery", uid="ABCDEF0123456789", channel=2, id="lens2")


def serving_both() -> Neolink:
    """A manager that believes neolink is running for both lenses."""
    manager = Neolink(on_login_rejected=lambda _id: None)
    manager._cameras = {LENS_1.id: LENS_1, LENS_2.id: LENS_2}
    manager._awake = {LENS_1.id: "main", LENS_2.id: "main"}
    manager._serving = dict(manager._awake)
    manager._proc = object()
    return manager


def line(cam_id: str, text: str, module: str = "neolink::rtsp::stream") -> str:
    return f"[2026-11-25T07:00:00Z INFO  {module}] {cam_id}: {text}"


def available(cam_id: str) -> str:
    return line(cam_id, f"Available at /{cam_id}/main, /{cam_id}/mainStream")


def test_lenses_connect_once_all_are_served_and_one_at_a_time():
    manager = serving_both()
    manager._on_line(available("lens1"))
    assert not manager.ready(LENS_1)  # the other lens isn't up yet
    manager._on_line(available("lens2"))
    assert manager.ready(LENS_1) and manager.ready(LENS_2)
    for lens in (LENS_1, LENS_2):
        manager.note(lens, "wake")
    manager.note(LENS_1, "connect#1")
    assert manager.ready(LENS_1) and not manager.ready(LENS_2)  # lens 2 waits its turn
    manager.note(LENS_1, "play ok")
    assert manager.ready(LENS_2)


def test_a_stalled_lens_does_not_hold_the_other_for_ever():
    manager = serving_both()
    for cam_id in ("lens1", "lens2"):
        manager._on_line(available(cam_id))
    manager.note(LENS_1, "wake")
    manager.note(LENS_1, "connect#1")
    assert not manager.ready(LENS_2)
    manager._connecting_since -= neolink_module.GATE_MAX_S + 0.1
    assert manager.ready(LENS_2)


def test_wake_timeline_is_logged_as_one_line(caplog):
    manager = serving_both()
    manager.note(LENS_1, "wake")
    manager._on_line(line("lens1", "Local discovery success ABCDEF0123456789 at 192.168.1.35:2015",
                          "neolink_core::bc_protocol"))
    assert manager.stage(LENS_1) == "found"
    manager._on_line(line("lens1", "Connected and logged in", "neolink::utils"))
    manager._on_line(available("lens1"))
    manager._on_line(available("lens2"))
    assert manager.stage(LENS_1) == "stream up"
    with caplog.at_level(logging.INFO, logger="reolink_viewer.neolink"):
        manager.note(LENS_1, "connect#1")
        manager.note(LENS_1, "first frame")
    timeline = [r.getMessage() for r in caplog.records if "wake" in r.getMessage()]
    assert len(timeline) == 1
    for step in ("found", "logged in", "stream up", "connect#1", "first frame"):
        assert step in timeline[0]


def test_uid_is_kept_out_of_the_log(caplog):
    manager = serving_both()
    with caplog.at_level(logging.DEBUG, logger="reolink_viewer.neolink"):
        manager._on_line(line("lens1", "Connecting to camera at Address: 192.168.1.35, UID: ABCDEF0123456789",
                              "neolink::utils"))
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "ABCDEF0123456789" not in logged and "ABCD…" in logged


def test_speed_up_library_is_found_next_to_neolink_and_can_be_switched_off(tmp_path, monkeypatch):
    binary = tmp_path / "neolink"
    binary.write_text("")
    monkeypatch.delenv("REOLINK_VIEWER_NEOLINK_SHIM", raising=False)
    monkeypatch.setattr(neolink_module.Path, "home", lambda: tmp_path / "nohome")
    assert find_shim(str(binary)) in (None, str(neolink_module.Path(neolink_module.__file__).resolve().parent.parent
                                                / neolink_module.SHIM_NAME))
    shim = tmp_path / neolink_module.SHIM_NAME
    shim.write_text("")
    assert find_shim(str(binary)) == str(shim)
    assert find_shim(None) is None
    monkeypatch.setenv("REOLINK_VIEWER_NEOLINK_SHIM", "0")
    assert find_shim(str(binary)) is None
    other = tmp_path / "other.so"
    other.write_text("")
    monkeypatch.setenv("REOLINK_VIEWER_NEOLINK_SHIM", str(other))
    assert find_shim(str(binary)) == str(other)


def test_speed_up_library_is_preloaded(monkeypatch):
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    assert "LD_PRELOAD" not in _environment(None)
    assert _environment("/x/shim.so")["LD_PRELOAD"] == "/x/shim.so"
    monkeypatch.setenv("LD_PRELOAD", "/other.so")
    assert _environment("/x/shim.so")["LD_PRELOAD"] == "/x/shim.so:/other.so"


def test_speed_up_is_dropped_after_a_wake_that_gave_no_video():
    manager = serving_both()
    manager._on_line(neolink_module.SHIM_LOADED)
    assert manager._shim_active
    for cam_id in ("lens1", "lens2"):
        manager._on_line(available(cam_id))
    manager.note(LENS_1, "wake")
    manager.note(LENS_1, "connect#1")
    manager.note(LENS_1, "gave up (503)")
    assert manager._shim_off  # the next wake starts neolink without it


def test_a_wake_that_fails_before_the_stream_is_up_keeps_the_speed_up():
    manager = serving_both()
    manager._on_line(neolink_module.SHIM_LOADED)
    manager.note(LENS_1, "wake")
    manager.note(LENS_1, "gave up (not ready within 60 s)")  # the camera never answered
    assert not manager._shim_off


def test_history_length_experiment_goes_into_the_config(monkeypatch):
    monkeypatch.setattr(neolink_module, "NEOLINK_BUFFER_MS", 100)
    conf = tomllib.loads(config_text([(LENS_1, "main")]))
    assert conf["cameras"][0]["buffer_duration"] == 300  # never below neolink's safe minimum


def test_cameras_no_longer_battery_are_dropped_from_the_awake_set():
    manager = serving_both()
    manager._sync_id = 1  # keep apply() from scheduling work on a main loop
    manager.apply([LENS_1])
    assert set(manager._awake) == {"lens1"}
