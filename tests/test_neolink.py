import tomllib

from reolink_viewer.config import NEOLINK_PORT, Camera
from reolink_viewer.neolink import config_text


def test_config_serves_battery_cameras_locally_on_demand():
    cam = Camera(name="Driveway", protocol="battery", uid=" ABCDEF0123456789 ",
                 password='p"a\\ss!word', id="abc123")
    conf = tomllib.loads(config_text([cam]))
    assert conf["bind"] == "127.0.0.1"
    assert conf["bind_port"] == NEOLINK_PORT
    (entry,) = conf["cameras"]
    assert entry["name"] == "abc123"  # the RTSP path, unlike the name never changes
    assert entry["uid"] == "ABCDEF0123456789"
    assert entry["password"] == 'p"a\\ss!word'
    # Never through Reolink's servers, and only awake while watched.
    assert entry["discovery"] == "local"
    assert entry["push_notifications"] is False
    assert entry["pause"] == {"on_client": True}
    assert entry["idle_disconnect"] is True
    assert entry["use_splash"] is False
