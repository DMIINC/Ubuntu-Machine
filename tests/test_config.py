import os
import stat

from reolink_viewer.config import (
    Camera,
    Settings,
    build_url,
    load_settings,
    redact_url,
    save_settings,
)


def test_rtsp_url_default_sub():
    cam = Camera(name="Front", host="192.168.1.50", password="secret")
    assert build_url(cam) == "rtsp://admin:secret@192.168.1.50:554/h264Preview_01_sub"


def test_rtsp_url_main_h265_nvr_channel():
    cam = Camera(name="Yard", host="10.0.0.9", password="p", channel=3, codec="h265")
    assert build_url(cam, "main") == "rtsp://admin:p@10.0.0.9:554/h265Preview_03_main"


def test_rtsp_ext_falls_back_to_sub():
    cam = Camera(name="x", host="h", stream="ext")
    assert build_url(cam).endswith("/h264Preview_01_sub")


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


def test_unknown_keys_ignored():
    cam = Camera.from_dict({"name": "A", "host": "h", "future_field": 1})
    assert cam.name == "A"


def test_missing_config_gives_defaults(tmp_path):
    assert load_settings(tmp_path / "nope.json").cameras == []
