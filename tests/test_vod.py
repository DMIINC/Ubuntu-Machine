import logging
import os
import shutil
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from reolink_viewer import vod

from fake_reolink_api import FakeCamera

CLIP = bytes(range(256)) * 2048  # 512 KB that don't look like a camera's JSON error
TOKEN = "tok-s3cr3t"


def camera(**options) -> FakeCamera:
    fake = FakeCamera(clip_bytes=CLIP, **options)
    fake.tokens.add(TOKEN)
    return fake


def url(fake: FakeCamera, token: str = TOKEN) -> str:
    return (f"{'https' if fake.https else 'http'}://127.0.0.1:{fake.port}/cgi-bin/api.cgi"
            f"?cmd=Playback&source=Mp4Record/x.mp4&output=x.mp4&token={token}")


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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


@pytest.fixture
def cache(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path / "cache" / "reolink-viewer" / "clips"


# ------------------------------------------------------------------ probe

def test_probe_server_that_honours_range():
    with camera() as fake:
        result = vod.probe(url(fake))
        assert result == vod.Probe(ok=True, ranges=True, size=len(CLIP))
        assert len(fake.paths) == 1  # one request, no login


def test_probe_server_that_ignores_range():
    with camera(ranges=False) as fake:
        assert vod.probe(url(fake)) == vod.Probe(ok=True, ranges=False, size=len(CLIP))


def test_probe_server_that_does_not_say_the_size():
    with camera(ranges=False, content_length=False) as fake:
        assert vod.probe(url(fake)) == vod.Probe(ok=True, ranges=False, size=None)


def test_probe_reports_a_cameras_json_error():
    with camera() as fake:
        result = vod.probe(url(fake, token="expired"))
        assert (result.ok, result.kind) == (False, "camera")
        assert "please login first" in result.text


def test_probe_http_error():
    with camera() as fake:
        result = vod.probe(f"http://127.0.0.1:{fake.port}/nothing/here.mp4")
        assert (result.ok, result.kind) == (False, "http")
        assert "404" in result.text


def test_probe_unreachable_camera():
    result = vod.probe(f"http://127.0.0.1:{closed_port()}/clip.mp4", timeout=3)
    assert (result.ok, result.kind) == (False, "network")
    assert "connection refused" in result.text


def test_probe_does_not_follow_redirects():
    with camera() as target:
        with camera(redirect_to=f"http://127.0.0.1:{target.port}") as fake:
            result = vod.probe(url(fake))
        assert (result.ok, result.kind) == (False, "http")
        assert target.paths == []  # the token went nowhere else


def test_probe_https_with_a_self_signed_certificate(certificate):
    with camera(certfile=certificate[0], keyfile=certificate[1]) as fake:
        assert vod.probe(url(fake)) == vod.Probe(ok=True, ranges=True, size=len(CLIP))


def test_probe_tls_failure():
    with camera() as fake:  # speaks plain HTTP: the handshake can't succeed
        result = vod.probe(url(fake).replace("http://", "https://"), timeout=3)
        assert (result.ok, result.kind) == (False, "tls")


def test_probe_only_takes_web_addresses(tmp_path):
    with pytest.raises(ValueError):
        vod.probe(f"file://{tmp_path}/clip.mp4")


def test_the_token_stays_out_of_the_log(caplog, tmp_path):
    with camera() as fake, caplog.at_level(logging.DEBUG, logger="reolink_viewer.vod"):
        vod.probe(url(fake))
        vod.probe(url(fake) + "x")  # a refusal is logged too
        Job().fetch(url(fake), tmp_path / "clip")
    assert "token=***" in caplog.text
    assert TOKEN not in caplog.text


# --------------------------------------------------------------- download

class Job:
    """Runs a Download with its callbacks called straight from the worker thread."""

    def __init__(self):
        self.progress: list[tuple[int, int | None]] = []
        self.done: Path | None = None
        self.failed: tuple[str, str] | None = None
        self.ended = threading.Event()

    def start(self, address: str, path: Path, **options) -> vod.Download:
        return vod.Download(
            address, path, lambda function, *args: function(*args),
            on_progress=lambda received, total: self.progress.append((received, total)),
            on_done=self._on_done, on_failed=self._on_failed, **options).start()

    def fetch(self, address: str, path: Path, **options) -> "Job":
        self.start(address, path, **options)
        assert self.ended.wait(10)
        return self

    def _on_done(self, path: Path) -> None:
        self.done = path
        self.ended.set()

    def _on_failed(self, kind: str, text: str) -> None:
        self.failed = (kind, text)
        self.ended.set()


def wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_download_with_progress(tmp_path):
    target = tmp_path / "clips" / "one.clip"
    with camera(ranges=False) as fake:
        job = Job().fetch(url(fake), target)
    assert job.done == target and job.failed is None
    assert target.read_bytes() == CLIP
    assert mode(target) == 0o600
    received = [count for count, _ in job.progress]
    assert len(received) > 1 and received == sorted(received) and received[-1] == len(CLIP)
    assert {total for _, total in job.progress} == {len(CLIP)}
    assert os.listdir(target.parent) == ["one.clip"]  # no temporary file left


def test_download_without_content_length(tmp_path):
    with camera(content_length=False) as fake:
        job = Job().fetch(url(fake), tmp_path / "clip")
    assert job.done and (tmp_path / "clip").read_bytes() == CLIP
    assert job.progress[-1] == (len(CLIP), None)


def test_download_over_https(tmp_path, certificate):
    with camera(certfile=certificate[0], keyfile=certificate[1]) as fake:
        job = Job().fetch(url(fake), tmp_path / "clip")
    assert job.done and (tmp_path / "clip").read_bytes() == CLIP


def test_download_cut_short_is_an_error_and_leaves_no_file(tmp_path):
    with camera(cut_after=100_000) as fake:
        job = Job().fetch(url(fake), tmp_path / "clip")
    assert job.done is None and job.failed[0] == "network"
    assert "100000 of 524288" in job.failed[1]
    assert os.listdir(tmp_path) == []


def test_download_keeps_the_complete_file_it_replaces_until_done(tmp_path):
    target = tmp_path / "clip"
    target.write_bytes(b"earlier download")
    with camera(cut_after=100_000) as fake:
        Job().fetch(url(fake), target)
    assert target.read_bytes() == b"earlier download"


def test_download_cancel(tmp_path):
    with camera(rate=200_000) as fake:  # 2.6 s for the whole clip
        job = Job()
        download = job.start(url(fake), tmp_path / "clip")
        assert wait_for(lambda: job.progress)
        download.cancel()
        heard = len(job.progress)
        assert wait_for(lambda: os.listdir(tmp_path) == [])  # the partial file is deleted
        time.sleep(0.2)
    assert not job.ended.is_set() and len(job.progress) == heard  # and the owner hears nothing more
    assert download.received < len(CLIP)


def test_download_reports_a_cameras_json_error(tmp_path):
    with camera() as fake:
        job = Job().fetch(url(fake, token="expired"), tmp_path / "clip")
    assert job.failed[0] == "camera" and "please login first" in job.failed[1]
    assert os.listdir(tmp_path) == []


def test_download_http_error_and_unreachable_camera(tmp_path):
    with camera() as fake:
        job = Job().fetch(f"http://127.0.0.1:{fake.port}/nothing.mp4", tmp_path / "clip")
    assert job.failed[0] == "http"
    job = Job().fetch(f"http://127.0.0.1:{closed_port()}/clip.mp4", tmp_path / "clip", timeout=3)
    assert job.failed[0] == "network"
    assert os.listdir(tmp_path) == []


def test_download_that_cannot_be_saved(tmp_path):
    (tmp_path / "file").write_bytes(b"")
    with camera() as fake:
        job = Job().fetch(url(fake), tmp_path / "file" / "clip")  # a folder can't be made there
    assert job.failed[0] == "disk"


def test_progress_reports_wait_one_at_a_time(tmp_path):
    waiting: list[tuple] = []
    seen: list[tuple[int, int | None]] = []
    ended = threading.Event()
    with camera() as fake:
        vod.Download(url(fake), tmp_path / "clip", lambda *call: waiting.append(call),
                     on_progress=lambda received, total: seen.append((received, total)),
                     on_done=lambda _path: ended.set()).start()
        assert wait_for(lambda: len(waiting) == 2)  # not one per 64 KB read
    for function, *args in waiting:
        assert function(*args) is False  # GLib.idle_add must not repeat them
    assert seen == [(len(CLIP), len(CLIP))] and ended.is_set()


# ------------------------------------------------------------------ cache

def test_cache_folder_and_files_are_private(cache):
    name = "Mp4Record/2026-11-24/RecS01_20261124_080000_080100_6D28808_1A468F9.mp4"
    with camera() as fake:
        target = vod.cache_path(f"cam1/{name}")
        Job().fetch(url(fake), target)
    assert vod.cache_dir() == cache == target.parent
    assert mode(cache) == 0o700 and mode(target) == 0o600
    assert target == vod.cache_path(f"cam1/{name}") != vod.cache_path(f"cam2/{name}")


def test_cache_folder_is_made_private_again(cache):
    cache.mkdir(parents=True, mode=0o755)
    os.chmod(cache, 0o755)
    assert mode(vod.cache_dir()) == 0o700


def aged(path: Path, size: int, age: float) -> Path:
    path.write_bytes(b"\0" * size)
    os.utime(path, (time.time() - age, time.time() - age))
    return path


def test_trim_cache_deletes_the_oldest_first(cache):
    old = aged(vod.cache_path("old"), 400, 300)
    middle = aged(vod.cache_path("middle"), 400, 200)
    new = aged(vod.cache_path("new"), 400, 100)
    vod.trim_cache(limit=900)
    assert (old.exists(), middle.exists(), new.exists()) == (False, True, True)
    vod.trim_cache(limit=500, keep=middle)
    assert (middle.exists(), new.exists()) == (True, False)
    vod.trim_cache(limit=0, keep=middle)
    assert middle.exists()


def test_a_download_into_the_cache_keeps_it_under_the_cap(cache, monkeypatch):
    monkeypatch.setattr(vod, "CACHE_LIMIT_BYTES", len(CLIP) + 1000)
    old = aged(vod.cache_path("old"), 2000, 300)
    recent = aged(vod.cache_path("recent"), 500, 100)
    with camera() as fake:
        job = Job().fetch(url(fake), vod.cache_path("new"))
    assert job.done.exists() and recent.exists() and not old.exists()


def test_clear_cache(cache, tmp_path):
    vod.clear_cache()  # nothing there yet: fine, and it doesn't create the folder
    assert not cache.exists()
    clip = aged(vod.cache_path("a"), 10, 0)
    part = aged(cache / "abc.clip.x1y2.part", 10, 0)
    other = aged(cache / "notes.txt", 10, 0)
    vod.clear_cache()
    assert not clip.exists() and not part.exists() and other.exists()
