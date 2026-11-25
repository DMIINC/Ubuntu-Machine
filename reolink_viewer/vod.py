"""Getting a recording off a camera: how it can be played, downloading it, a private cache.

A recording plays straight from the camera only if the camera's web server can
jump around in the file (HTTP Range): playbin then starts within a second and
seeks at will. Without Range an MP4 whose index sits at its end doesn't start
at all and seeking fails, so the clip is downloaded first and played from disk.
probe() finds out which it is with one request, and puts a camera's refusal
into plain words before GStreamer gets involved.

Size matters too: GStreamer reads an MP4 of up to 4 MB to its end before the
first picture when the index is at the end, Range or not (measured at 1 MB/s:
3.8 MB started after 3.9 s, 4.6 MB after 0.8 s). For a clip that small,
downloading costs the same wait and can show its progress.

Standard library only, no GTK: Download reports back through a function its
owner supplies (a window passes GLib.idle_add).

These URLs carry the session token: log them only through config.redact_url.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import ssl
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from . import api
from .config import redact_url

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 10
DOWNLOAD_TIMEOUT_S = 15  # per read: a camera silent for this long has stalled
CHUNK_BYTES = 64 * 1024
HEAD_BYTES = 2048  # enough to hold a camera's JSON error answer
CACHE_LIMIT_BYTES = 1024 ** 3


@dataclass(frozen=True)
class Probe:
    """What one request told about a recording's URL."""

    ok: bool
    ranges: bool = False  # the server can jump around in the file: play it straight from the camera
    size: int | None = None  # bytes, if the server said
    kind: str = ""  # why not ok: "camera" (it answered with an error), "tls", "http" or "network"
    text: str = ""  # the same, for the user


class _Failed(Exception):
    """A request went wrong; str(exc) is written for the user."""

    def __init__(self, kind: str, text: str):
        super().__init__(text)
        self.kind = kind


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the URL's token must not travel to wherever one points."""

    def redirect_request(self, *_args, **_kwargs):
        return None


def _why(reason: object) -> str:
    if isinstance(reason, TimeoutError) or "timed out" in str(reason):
        return "no answer"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    return str(reason) or type(reason).__name__


def _open(url: str, headers: dict[str, str], timeout: float) -> http.client.HTTPResponse:
    """Send one GET and return the open answer, or raise _Failed.

    Only a failed TLS handshake leads to a second try (with the old TLS
    settings of older firmware): nothing has reached the camera's API by then.
    """
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"not an http(s) URL: {redact_url(url)}")
    request = urllib.request.Request(url, headers=headers)
    refused: object = None
    for legacy in ((False, True) if scheme == "https" else (False,)):
        handlers: list = [_NoRedirects(), urllib.request.ProxyHandler({})]  # cameras are on the LAN
        if scheme == "https":
            handlers.append(urllib.request.HTTPSHandler(context=api.tls_context(legacy)))
        try:
            return urllib.request.build_opener(*handlers).open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                raise _Failed("http", "The camera can't find this recording (HTTP 404).") from None
            raise _Failed("http", f"The camera answered with an error (HTTP {exc.code}).") from None
        except (OSError, http.client.HTTPException) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if not isinstance(reason, ssl.SSLError):
                raise _Failed("network", f"Couldn't reach the camera ({_why(reason)}).") from None
            refused = reason
    raise _Failed("tls", f"Couldn't set up a secure connection to the camera ({refused}).")


def _refusal(head: bytes) -> str | None:
    """What to tell the user if these first bytes are a camera's error answer.

    Cameras refuse with HTTP 200 and a JSON body (e.g. "please login first"
    once the token has run out). No video file starts with [ or {.
    """
    if head[:1] not in (b"[", b"{"):
        return None
    try:
        answer = json.loads(head.decode("utf-8", "replace"))
        item = answer[0] if isinstance(answer, list) else answer
        detail = str((item.get("error") or {}).get("detail") or "")
    except (ValueError, AttributeError, LookupError, TypeError):
        detail = ""
    return f"The camera refused to send the recording{f' ({detail})' if detail else ''}."


def _length(answer: http.client.HTTPResponse) -> int | None:
    value = answer.headers.get("Content-Length") or ""
    return int(value) if value.isdigit() else None


def probe(url: str, timeout: float = PROBE_TIMEOUT_S) -> Probe:
    """Ask for the first byte of a recording to learn how it can be played.

    One request, no login. Blocks: call it from a worker thread.
    """
    try:
        with _open(url, {"Range": "bytes=0-0"}, timeout) as answer:
            if answer.status == 206:
                head = answer.read(1)
                total = (answer.headers.get("Content-Range") or "").rpartition("/")[2]
                result = Probe(True, True, int(total) if total.isdigit() else None)
            else:  # the whole file is on its way: only its start is read
                head = answer.read(HEAD_BYTES)
                result = Probe(True, False, _length(answer))
        refusal = _refusal(head)
        if refusal:
            result = Probe(False, kind="camera", text=refusal)
    except _Failed as exc:
        result = Probe(False, kind=exc.kind, text=str(exc))
    except (OSError, http.client.HTTPException) as exc:
        result = Probe(False, kind="network", text=f"The camera's answer broke off ({_why(exc)}).")
    if result.ok:
        log.info("%s: %s, %s bytes", redact_url(url),
                 "can be played from the camera" if result.ranges else "has to be downloaded (no Range)",
                 result.size if result.size is not None else "unknown")
    else:
        log.warning("%s: %s (%s)", redact_url(url), result.text, result.kind)
    return result


def _call(function: Callable[..., object], *args: object) -> bool:
    """Run a callback once: GLib.idle_add would repeat one that returns something true."""
    function(*args)
    return False


class Download:
    """Fetches a recording into a file on a worker thread.

    deliver(function, *args) has to get function(*args) called where the owner
    wants to hear back; a window passes GLib.idle_add. Through it the owner
    gets on_progress(bytes_so_far, total_or_None) now and then, and at the end
    either on_done(path) or on_failed(kind, text): kind as in Probe, or "disk".
    After cancel() it hears nothing more.

    The data goes to a temporary file beside `path`, which takes that name only
    once it is complete: `path` never holds half a recording, and a failed or
    cancelled download leaves nothing behind.
    """

    def __init__(self, url: str, path: Path | str, deliver: Callable[..., object],
                 on_progress: Callable[[int, int | None], None] | None = None,
                 on_done: Callable[[Path], None] | None = None,
                 on_failed: Callable[[str, str], None] | None = None,
                 timeout: float = DOWNLOAD_TIMEOUT_S):
        self.url = url
        self.path = Path(path)
        self.received = 0
        self.total: int | None = None  # None: the server didn't say
        self._deliver = deliver
        self._on_progress = on_progress
        self._on_done = on_done
        self._on_failed = on_failed
        self._timeout = timeout
        self._cancelled = threading.Event()
        self._progress_queued = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "Download":
        self._thread.start()
        return self

    def cancel(self) -> None:
        """Stop, and drop what was fetched so far. Does nothing once the owner has
        heard the outcome; a download that had just completed keeps its file."""
        self._cancelled.set()

    def _run(self) -> None:
        log.info("downloading %s", redact_url(self.url))
        failure: _Failed | None = None
        part = ""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, part = tempfile.mkstemp(  # created readable by the owner only
                prefix=f"{self.path.name}.", suffix=".part", dir=self.path.parent)
            with os.fdopen(handle, "wb") as out:
                self._fetch(out)
            if not self._cancelled.is_set():
                os.replace(part, self.path)
                part = ""
        except _Failed as exc:
            failure = exc
        except OSError as exc:
            failure = _Failed("disk", f"Couldn't save the recording ({exc.strerror or exc}).")
        if part:
            _remove(Path(part))
        if self._cancelled.is_set():
            log.info("download cancelled after %d bytes", self.received)
            return
        if failure is None:
            log.info("downloaded %d bytes", self.received)
            if self.path.parent == _cache_folder():
                trim_cache(keep=self.path)
        else:
            log.warning("download failed after %d bytes: %s (%s)", self.received, failure, failure.kind)
        self._deliver(_call, self._report_end, failure)

    def _fetch(self, out: BinaryIO) -> None:
        with _open(self.url, {}, self._timeout) as answer:
            self.total = _length(answer)
            while not self._cancelled.is_set():
                try:
                    piece = answer.read1(CHUNK_BYTES)
                except (OSError, http.client.HTTPException) as exc:
                    raise _Failed(
                        "network", f"The connection to the camera broke off ({_why(exc)}).") from None
                if not piece:
                    break
                if not self.received:
                    refusal = _refusal(piece)
                    if refusal:
                        raise _Failed("camera", refusal)
                out.write(piece)
                self.received += len(piece)
                self._queue_progress()
        if not self._cancelled.is_set() and self.total is not None and self.received < self.total:
            # The server closed early; a file that plays to the middle is worse than none.
            raise _Failed("network", "The camera stopped sending before the recording was complete "
                                     f"({self.received} of {self.total} bytes).")

    def _queue_progress(self) -> None:
        # One report waiting at a time: a download makes thousands of reads.
        if self._on_progress is not None and not self._progress_queued:
            self._progress_queued = True
            self._deliver(_call, self._report_progress)

    def _report_progress(self) -> None:
        self._progress_queued = False
        if not self._cancelled.is_set() and self._on_progress is not None:
            self._on_progress(self.received, self.total)

    def _report_end(self, failure: _Failed | None) -> None:
        if self._cancelled.is_set():
            return
        if failure is None:
            if self._on_done is not None:
                self._on_done(self.path)
        elif self._on_failed is not None:
            self._on_failed(failure.kind, str(failure))


# ------------------------------------------------------------------ cache
#
# Downloaded recordings are copies of what the camera keeps, and they show the
# inside of a home: they live in a folder only the user can open, and are
# deleted when the app exits.

def _cache_folder() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "reolink-viewer" / "clips"


def cache_dir() -> Path:
    """The folder downloaded recordings wait in, created private if need be."""
    folder = _cache_folder()
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def cache_path(key: str) -> Path:
    """Where the recording called `key` is (or would be) kept. Any text that tells
    clips apart will do, e.g. a camera's id followed by Recording.key."""
    return cache_dir() / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}.clip"


def _cached(suffixes: tuple[str, ...]) -> list[Path]:
    try:
        return [entry for entry in _cache_folder().iterdir()
                if entry.suffix in suffixes and entry.is_file()]
    except OSError:  # no cache yet
        return []


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def clear_cache() -> None:
    """Delete every downloaded recording, complete or not (call when the app exits)."""
    for path in _cached((".clip", ".part")):
        _remove(path)


def trim_cache(limit: int | None = None, keep: Path | None = None) -> None:
    """Delete the oldest recordings until the cache holds at most `limit` bytes
    (CACHE_LIMIT_BYTES unless given). `keep`, the one about to be played, stays."""
    limit = CACHE_LIMIT_BYTES if limit is None else limit
    sizes: dict[Path, tuple[float, int]] = {}
    for path in _cached((".clip",)):
        try:
            info = path.stat()
        except OSError:
            continue
        sizes[path] = (info.st_mtime, info.st_size)
    total = sum(size for _, size in sizes.values())
    for path in sorted(sizes, key=lambda p: sizes[p][0]):
        if total <= limit:
            break
        if path != keep:
            _remove(path)
            total -= sizes[path][1]
