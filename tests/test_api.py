import shutil
import socket
import subprocess
import urllib.request
from datetime import date, datetime

import pytest

from reolink_viewer import api
from reolink_viewer.api import ApiError, CameraApi, LoginRejected, NotReachable, NotSupported
from reolink_viewer.config import Camera, redact_url

from fake_reolink_api import FakeCamera, clip


@pytest.fixture(autouse=True)
def _fresh_memory():
    api._rejected.clear()
    api._sessions.clear()
    yield
    api._rejected.clear()
    api._sessions.clear()


def cam(password="pw", **kwargs) -> Camera:
    return Camera(name="Test", host="127.0.0.1", password=password, **kwargs)


def client(fake: FakeCamera, password="pw", **kwargs) -> CameraApi:
    return CameraApi(cam(password, **kwargs), ports=((fake.port, False),))


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_login_once_and_reuse_the_token():
    with FakeCamera() as fake:
        session = client(fake)
        session.login()
        session.login()
        session.recording_days(2026, 11)
        assert fake.login_attempts == 1
        assert session.logged_in


def test_password_travels_only_in_the_login_body():
    with FakeCamera(password="s3cr3t!") as fake:
        session = client(fake, "s3cr3t!")
        fake.add_clips(clip(datetime(2026, 11, 24, 8)))
        rec = session.recordings(date(2026, 11, 24))[0]
        url = session.playback_url(rec)
        assert all("s3cr3t" not in path for path in fake.paths)
        assert "s3cr3t" not in url and "token=" in url
        assert "token=***" in redact_url(url)


def test_rejected_login_is_tried_exactly_once():
    with FakeCamera(password="right") as fake:
        session = client(fake, "wrong")
        with pytest.raises(LoginRejected):
            session.login()
        with pytest.raises(LoginRejected):
            session.recordings(date(2026, 11, 24))
        # A new session object for the same login doesn't ask the camera either.
        with pytest.raises(LoginRejected):
            client(fake, "wrong").login()
        assert fake.login_attempts == 1
        # Editing the password makes it worth one more try.
        client(fake, "right").login()
        assert fake.login_attempts == 2


def test_a_rejection_never_moves_on_to_the_next_port():
    with FakeCamera(password="right") as first, FakeCamera(password="right") as second:
        session = CameraApi(cam("wrong"), ports=((first.port, False), (second.port, False)))
        with pytest.raises(LoginRejected):
            session.login()
        assert (first.login_attempts, second.login_attempts) == (1, 0)


def test_locked_account_is_reported_and_not_retried():
    with FakeCamera(lock_after=0) as fake:
        session = client(fake)
        with pytest.raises(LoginRejected, match="locked"):
            session.login()
        with pytest.raises(LoginRejected):
            session.login()
        assert fake.login_attempts == 1


def test_unreachable_port_moves_on_to_the_next():
    with FakeCamera() as fake:
        session = CameraApi(cam(), ports=((closed_port(), False), (fake.port, False)))
        session.login()
        assert fake.login_attempts == 1


def test_nothing_listening_is_not_reachable():
    session = CameraApi(cam(), ports=((closed_port(), True), (closed_port(), False)))
    with pytest.raises(NotReachable):
        session.login()


def test_redirects_are_not_followed_with_the_login():
    with FakeCamera() as target:
        with FakeCamera(redirect_to=f"http://127.0.0.1:{target.port}") as fake:
            with pytest.raises(NotReachable):
                client(fake).login()
        assert target.login_attempts == 0


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    folder = tmp_path_factory.mktemp("cert")
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=camera", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True)
    return str(cert), str(key)


def test_https_with_a_self_signed_certificate(certificate):
    with FakeCamera(certfile=certificate[0], keyfile=certificate[1]) as fake:
        session = CameraApi(cam(api_port=fake.port))  # api_port: HTTPS first, then HTTP
        session.login()
        assert session.uses_https and fake.login_attempts == 1


def test_falls_back_to_http_when_the_port_is_not_tls():
    with FakeCamera() as fake:
        session = CameraApi(cam(api_port=fake.port))
        session.login()
        assert not session.uses_https and fake.login_attempts == 1


def test_recording_days():
    with FakeCamera() as fake:
        fake.add_clips(clip(datetime(2026, 11, 3, 9)), clip(datetime(2026, 11, 24, 8)),
                       clip(datetime(2026, 10, 30, 8)), clip(datetime(2026, 11, 5, 8), stream="sub"))
        session = client(fake)
        assert session.recording_days(2026, 11) == [3, 24]
        assert session.recording_days(2026, 11, "sub") == [5]
        assert session.recording_days(2026, 9) == []


def test_recordings_are_sorted_parsed_and_paged():
    with FakeCamera(page_size=2) as fake:  # firmware that caps each search at 2 files
        hours = (17, 8, 12, 21, 3)
        fake.add_clips(*(clip(datetime(2026, 11, 24, h), 90, flags="6D28C08") for h in hours))
        fake.add_clips(clip(datetime(2026, 11, 23, 23, 30)), clip(datetime(2026, 11, 25, 0, 10)))
        recs = client(fake).recordings(date(2026, 11, 24))
        assert [r.start.hour for r in recs] == sorted(hours)
        first = recs[0]
        assert first.name.startswith("Mp4Record/2026-11-24/RecM01_20261124_030000_")
        assert first.duration.total_seconds() == 90
        assert first.size == 1048576 and first.stream == "main"
        assert first.triggers == ("Person", "Motion")


def test_a_day_without_recordings_is_empty():
    with FakeCamera() as fake:
        assert client(fake).recordings(date(2026, 11, 24)) == []


def test_playback_url_serves_the_clip():
    with FakeCamera(clip_bytes=b"MP4DATA" * 100) as fake:
        fake.add_clips(clip(datetime(2026, 11, 24, 8)))
        session = client(fake)
        rec = session.recordings(date(2026, 11, 24))[0]
        url = session.playback_url(rec)
        assert "cmd=Playback" in url and "start=20261124080000" in url
        assert f"source={rec.name}" in url
        with urllib.request.urlopen(url) as response:
            assert response.read() == b"MP4DATA" * 100
        request = urllib.request.Request(url, headers={"Range": "bytes=7-13"})
        with urllib.request.urlopen(request) as response:
            assert response.status == 206 and response.read() == b"MP4DATA"


def test_nvr_clips_play_over_flv_only_with_a_url_safe_login():
    with FakeCamera() as fake:
        nvr_clip = api.Recording(datetime(2026, 11, 24, 8), datetime(2026, 11, 24, 9), 0, "main", "")
        url = client(fake).playback_url(nvr_clip)
        assert "/flv?" in url and "stream=playback.bcs" in url and "start=20261124080000" in url
        assert "password=***" in redact_url(url)
    with FakeCamera(password="p#ss") as fake:
        with pytest.raises(NotSupported):
            client(fake, "p#ss").playback_url(nvr_clip)


def test_expired_session_logs_in_again_once():
    with FakeCamera() as fake:
        session = client(fake)
        session.login()
        fake.tokens.clear()  # the camera forgot the session (e.g. it restarted)
        assert session.recording_days(2026, 11) == []
        assert fake.login_attempts == 2


def test_zoom_and_focus():
    with FakeCamera() as fake:
        session = client(fake, channel=2)
        state = session.zoom_focus()
        assert (state.zoom, state.zoom_min, state.zoom_max) == (10, 0, 33)
        assert (state.focus, state.focus_min, state.focus_max) == (120, 0, 223)
        assert state.autofocus is True
        session.set_zoom(20)
        session.set_focus(77)
        session.set_autofocus(False)
        assert fake.moves == [("ZoomPos", 20), ("FocusPos", 77)]
        state = session.zoom_focus()
        assert (state.zoom, state.focus, state.autofocus) == (20, 77, False)


def test_camera_without_motorised_lens():
    with FakeCamera(zoom=False) as fake:
        with pytest.raises(NotSupported):
            client(fake).zoom_focus()


def test_camera_without_autofocus_switch():
    with FakeCamera(autofocus=False) as fake:
        assert client(fake).zoom_focus().autofocus is None


def test_battery_and_custom_cameras_have_no_api():
    battery = Camera(name="Driveway", protocol="battery", uid="ABC", host="192.168.1.35")
    custom = Camera(name="Other", protocol="custom", custom_url="rtsp://x/y")
    assert api.has_api(battery) and api.has_api(custom)
    assert api.has_api(cam()) is None
    with pytest.raises(NotSupported):
        CameraApi(battery).login()


def test_login_is_risky_for_symbols_reolink_has_refused():
    assert not api.login_is_risky(cam("Abc123!"))
    assert api.login_is_risky(cam("Abc#123"))


def test_shared_session_is_replaced_when_the_login_changes():
    first = api.session_for(cam("pw", id="c1"))
    assert api.session_for(cam("pw", id="c1")) is first
    assert api.session_for(cam("other", id="c1")) is not first


def test_logout_all_frees_the_sessions():
    with FakeCamera() as fake:
        session = api.session_for(cam(id="c1", api_port=fake.port))
        session.login()
        assert fake.tokens
        api.logout_all()
        assert not fake.tokens and "Logout" in fake.commands


def test_logout_without_a_session_sends_nothing():
    with FakeCamera() as fake:
        client(fake).logout()
        assert fake.commands == []


def test_api_errors_are_reported():
    with FakeCamera() as fake:
        session = client(fake)
        session.login()
        with pytest.raises(ApiError):
            session._command("NoSuchCommand", {})


def test_parse_triggers():
    name = "Mp4Record/2020-12-22/RecM01_20201222_075939_080140_{}_1A468F9.mp4"
    assert api.parse_triggers(name.format("6D28808")) == ("Motion",)
    assert api.parse_triggers(name.format("6D28C08")) == ("Person", "Motion")
    assert api.parse_triggers(name.format("6D28908")) == ("Vehicle", "Motion")
    assert api.parse_triggers("20230417015957") == ()
