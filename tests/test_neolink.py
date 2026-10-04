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
