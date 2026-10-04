import os
import stat

from reolink_viewer.config import (
    NEOLINK_PORT,
    Camera,
    Settings,
    build_url,
    load_settings,
    redact_url,
    resolve_stream,
    save_settings,
)


def test_rtsp_url_default_sub():
    cam = Camera(name="Front", host="192.168.1.50", password="secret")
    assert build_url(cam) == "rtsp://admin:secret@192.168.1.50:554/h264Preview_01_sub"


def test_rtsp_url_main_h265_nvr_channel():
    cam = Camera(name="Yard", host="10.0.0.9", password="p", channel=3, codec="h265")
    assert build_url(cam, "main") == "rtsp://admin:p@10.0.0.9:554/h265Preview_03_main"


def test_rtsp_camera_gets_balanced_stream_over_rtmp():
    cam = Camera(name="x", host="h", password="pw123", stream="ext")
    assert build_url(cam) == (
        "rtmp://h:1935/bcs/channel0_ext.bcs?channel=0&stream=0&user=admin&password=pw123"
    )


def test_balanced_stream_needs_rtmp_safe_login():
    # RTMP takes the login in the URL query and rejects anything needing escaping.
    assert resolve_stream(Camera(name="x", password="Abc123"), "ext") == "ext"
    assert resolve_stream(Camera(name="x", password="Abc#123"), "ext") == "sub"
    assert resolve_stream(Camera(name="x", password="Abc#123"), "main") == "main"
    # RTMP / FLV cameras were set up that way on purpose.
    assert resolve_stream(Camera(name="x", password="Abc#123", protocol="rtmp"), "ext") == "ext"


def test_exclamation_mark_goes_into_rtmp_url_literally():
    # Reolink doesn't decode the query: "%21" is rejected, "!" works.
    cam = Camera(name="x", host="h", password="12!34", stream="ext")
    assert resolve_stream(cam, "ext") == "ext"
    assert build_url(cam).endswith("&user=admin&password=12!34")


def test_password_special_chars_are_escaped():
    cam = Camera(name="x", host="h", password="p@ss:w/rd#1")
    url = build_url(cam)
    assert "p%40ss%3Aw%2Frd%231@h:554" in url
    assert redact_url(url) == "rtsp://admin:***@h:554/h264Preview_01_sub"


def test_rtmp_url():
    cam = Camera(name="x", host="192.168.1.5", protocol="rtmp", port=1935,
                 password="a&b", channel=2, stream="main")
    url = build_url(cam)
    assert url == (
        "rtmp://192.168.1.5:1935/bcs/channel1_main.bcs"
        "?channel=1&stream=0&user=admin&password=a%26b"
    )
    assert redact_url(url).endswith("user=admin&password=***")


def test_flv_url():
    cam = Camera(name="x", host="cam.local", protocol="flv", port=80, password="pw")
    assert build_url(cam) == (
        "http://cam.local:80/flv?port=1935&app=bcs&stream=channel0_sub.bcs"
        "&user=admin&password=pw"
    )


def test_custom_url_used_verbatim():
    cam = Camera(name="x", protocol="custom", custom_url=" rtsp://u:p@h/stream ")
    assert build_url(cam) == "rtsp://u:p@h/stream"


def test_missing_host_raises():
    try:
        build_url(Camera(name="x"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "sub" / "config.json"
    settings = Settings(columns=3, cameras=[Camera(name="A", host="h", password="pw")])
    save_settings(settings, path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    loaded = load_settings(path)
    assert loaded.columns == 3
    assert loaded.cameras[0].password == "pw"
    assert loaded.cameras[0].id == settings.cameras[0].id


def test_grid_stream_defaults_to_balanced_and_roundtrips(tmp_path):
    assert Settings.from_dict({}).grid_stream == "ext"  # configs from before the setting
    assert Settings.from_dict({"grid_stream": "bogus"}).grid_stream == "ext"
    path = tmp_path / "config.json"
    save_settings(Settings(grid_stream=""), path)
    assert load_settings(path).grid_stream == ""


def test_unknown_keys_ignored():
    cam = Camera.from_dict({"name": "A", "host": "h", "future_field": 1})
    assert cam.name == "A"


def test_missing_config_gives_defaults(tmp_path):
    assert load_settings(tmp_path / "nope.json").cameras == []


def test_battery_camera_plays_from_neolink():
    cam = Camera(name="Driveway", protocol="battery", uid="ABCDEF0123456789", id="abc123")
    assert cam.on_demand
    assert build_url(cam, "main") == f"rtsp://127.0.0.1:{NEOLINK_PORT}/abc123/mainStream"
    # No balanced stream: the grid plays sub.
    assert resolve_stream(cam, "ext") == "sub"
    assert build_url(cam, "sub").endswith("/abc123/subStream")
    assert not Camera(name="x").on_demand


def test_battery_camera_needs_uid():
    try:
        build_url(Camera(name="x", protocol="battery"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
