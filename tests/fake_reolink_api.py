"""A stand-in for a Reolink camera's HTTP(S) API, for tests and for trying the app without a camera.

It answers the commands the viewer uses, in the shapes reolink_aio documents:
Login / Logout, Search (recordings), Playback (serves a clip), GetZoomFocus /
StartZoomFocus, GetAutoFocus / SetAutoFocus.

As a program:  fake_reolink_api.py PORT [--password PW] [--https CERT KEY] [--clip FILE.mp4]
It then holds three clips for each of today and yesterday.
"""

from __future__ import annotations

import json
import secrets
import ssl
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def _time(moment: datetime) -> dict:
    return {"year": moment.year, "mon": moment.month, "day": moment.day,
            "hour": moment.hour, "min": moment.minute, "sec": moment.second}


def _moment(value: dict) -> datetime:
    return datetime(value["year"], value["mon"], value["day"], value["hour"], value["min"], value["sec"])


def clip(start: datetime, seconds: int = 60, stream: str = "main", flags: str = "6D28808") -> dict:
    """A recording entry as a camera's Search returns it."""
    end = start + timedelta(seconds=seconds)
    letter = "M" if stream == "main" else "S"
    name = (f"Mp4Record/{start:%Y-%m-%d}/Rec{letter}01_{start:%Y%m%d}_{start:%H%M%S}_"
            f"{end:%H%M%S}_{flags}_1A468F9.mp4")
    return {"StartTime": _time(start), "EndTime": _time(end), "name": name, "size": "1048576",
            "type": stream, "frameRate": 25, "height": 2160, "width": 3840}


class FakeCamera:
    def __init__(self, password: str = "pw", *, username: str = "admin", page_size: int | None = None,
                 zoom: bool = True, autofocus: bool = True, lock_after: int | None = None,
                 redirect_to: str | None = None, certfile: str | None = None, keyfile: str | None = None,
                 port: int = 0, clip_bytes: bytes = b"\x00\x00\x00\x18ftypmp42 fake clip bytes",
                 ranges: bool = True, content_length: bool = True, rate: int = 0,
                 cut_after: int | None = None):
        self.username, self.password = username, password
        # How Playback serves the clip: ranges=False ignores Range headers (always the
        # whole file), content_length=False leaves the size out, rate throttles to that
        # many bytes per second, cut_after drops the connection after that many bytes.
        self.ranges, self.content_length = ranges, content_length
        self.rate, self.cut_after = rate, cut_after
        self.page_size = page_size
        self.has_zoom, self.has_autofocus = zoom, autofocus
        self.lock_after = lock_after
        self.redirect_to = redirect_to
        self.clip_bytes = clip_bytes
        self.login_attempts = 0
        self.tokens: set[str] = set()
        self.paths: list[str] = []  # every request path, to prove what went into URLs
        self.commands: list[str] = []
        self.clips: list[dict] = []
        self.zoom, self.focus, self.autofocus_disabled = 10, 120, 0
        self.moves: list[tuple[str, int]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.https = bool(certfile)
        if certfile:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile, keyfile)
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.port = self._server.server_address[1]
        # A short poll so stop() returns at once (the default makes every test wait 0.5 s).
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.02,), daemon=True)

    def start(self) -> "FakeCamera":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FakeCamera":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    def add_clips(self, *clips: dict) -> None:
        self.clips.extend(clips)

    # ------------------------------------------------------------- commands

    def _answer(self, cmd: dict, token: str) -> dict:
        name = cmd.get("cmd", "")
        self.commands.append(name)

        def error(code: int, detail: str) -> dict:
            return {"cmd": name, "code": 1, "error": {"detail": detail, "rspCode": code}}

        if name == "Login":
            self.login_attempts += 1
            user = cmd.get("param", {}).get("User", {})
            if self.lock_after is not None and self.login_attempts > self.lock_after:
                return error(-1, "Login has been locked")
            if user.get("userName") != self.username or user.get("password") != self.password:
                return error(-7, "login failed")
            new = secrets.token_hex(6)
            self.tokens.add(new)
            return {"cmd": name, "code": 0, "value": {"Token": {"leaseTime": 3600, "name": new}}}
        if token not in self.tokens:
            return error(-6, "please login first")
        if name == "Logout":
            self.tokens.discard(token)
            return {"cmd": name, "code": 0, "value": {"rspCode": 200}}
        param = cmd.get("param", {})
        if name == "Search":
            return self._search(param.get("Search", {}))
        if name in ("GetZoomFocus", "StartZoomFocus") and not self.has_zoom:
            return error(-9, "not support")
        if name == "GetZoomFocus":
            item = {"cmd": name, "code": 0, "value": {"ZoomFocus": {
                "channel": param.get("channel", 0), "focus": {"pos": self.focus}, "zoom": {"pos": self.zoom}}}}
            if cmd.get("action") == 1:
                item["range"] = {"ZoomFocus": {"focus": {"pos": {"min": 0, "max": 223}},
                                               "zoom": {"pos": {"min": 0, "max": 33}}}}
            return item
        if name == "StartZoomFocus":
            move = param.get("ZoomFocus", {})
            self.moves.append((move.get("op"), move.get("pos")))
            if move.get("op") == "ZoomPos":
                self.zoom = move.get("pos")
            elif move.get("op") == "FocusPos":
                self.focus = move.get("pos")
            else:
                return error(-4, "param error")
            return {"cmd": name, "code": 0, "value": {"rspCode": 200}}
        if name in ("GetAutoFocus", "SetAutoFocus") and not self.has_autofocus:
            return error(-9, "not support")
        if name == "GetAutoFocus":
            return {"cmd": name, "code": 0, "value": {"AutoFocus": {
                "channel": param.get("channel", 0), "disable": self.autofocus_disabled}}}
        if name == "SetAutoFocus":
            self.autofocus_disabled = param.get("AutoFocus", {}).get("disable", 0)
            return {"cmd": name, "code": 0, "value": {"rspCode": 200}}
        return error(-9, "not support")

    def _search(self, search: dict) -> dict:
        start, end = _moment(search["StartTime"]), _moment(search["EndTime"])
        stream = search.get("streamType", "main")
        clips = [c for c in self.clips if c["type"] == stream]
        months = {(start.year, start.month), (end.year, end.month)}
        status = []
        for year, month in sorted(months):
            table = ["0"] * 31
            for c in clips:
                c_start = _moment(c["StartTime"])
                if (c_start.year, c_start.month) == (year, month):
                    table[c_start.day - 1] = "1"
            if "1" in table:
                status.append({"mon": month, "table": "".join(table), "year": year})
        result: dict = {"channel": search.get("channel", 0)}
        if status:
            result["Status"] = status
        if not search.get("onlyStatus"):
            files = sorted(
                (c for c in clips if _moment(c["EndTime"]) > start and _moment(c["StartTime"]) <= end),
                key=lambda c: _moment(c["StartTime"]))
            if self.page_size:
                files = files[:self.page_size]
            if files:
                result["File"] = files
        return {"cmd": "Search", "code": 0, "value": {"SearchResult": result}}

    # ----------------------------------------------------------------- HTTP

    def _handler(self):
        camera = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                pass

            def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_POST(self) -> None:
                camera.paths.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if camera.redirect_to:
                    self._send(302, b"", "text/html", {"Location": camera.redirect_to + self.path})
                    return
                query = parse_qs(urlsplit(self.path).query)
                token = (query.get("token") or [""])[0]
                try:
                    commands = json.loads(raw)
                except ValueError:
                    self._send(400, b"bad request", "text/html")
                    return
                answer = [camera._answer(cmd, token) for cmd in commands]
                # Real cameras label their JSON as text/html.
                self._send(200, json.dumps(answer).encode(), "text/html")

            def do_GET(self) -> None:
                camera.paths.append(self.path)
                if camera.redirect_to:
                    self._send(302, b"", "text/html", {"Location": camera.redirect_to + self.path})
                    return
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                if parts.path != "/cgi-bin/api.cgi" or (query.get("cmd") or [""])[0] != "Playback":
                    self._send(404, b"not found", "text/html")
                    return
                if (query.get("token") or [""])[0] not in camera.tokens:
                    body = json.dumps([{"cmd": "Playback", "code": 1,
                                        "error": {"detail": "please login first", "rspCode": -6}}])
                    self._send(200, body.encode(), "text/html")
                    return
                data = camera.clip_bytes
                header = self.headers.get("Range", "")
                if header.startswith("bytes=") and camera.ranges:
                    first, _, last = header[6:].partition("-")
                    begin = int(first or 0)
                    stop = min(int(last), len(data) - 1) if last else len(data) - 1
                    chunk = data[begin:stop + 1]
                    self._send_clip(206, chunk, {
                        "Content-Range": f"bytes {begin}-{stop}/{len(data)}", "Accept-Ranges": "bytes"})
                    return
                # (Real cameras misspell this one as "apolication/octet-stream".)
                self._send_clip(200, data, {"Accept-Ranges": "bytes"} if camera.ranges else {})

            def _send_clip(self, status: int, body: bytes, extra: dict) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "video/mp4")
                if camera.content_length:
                    self.send_header("Content-Length", str(len(body)))
                else:  # the body ends where the connection does
                    self.send_header("Connection", "close")
                    self.close_connection = True
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command == "HEAD":
                    return
                if camera.cut_after is not None and len(body) > camera.cut_after:
                    body = body[:camera.cut_after]
                    self.close_connection = True
                started = time.monotonic()
                try:
                    for sent in range(0, len(body), 16384):
                        self.wfile.write(body[sent:sent + 16384])
                        if camera.rate:
                            ahead = (sent + 16384) / camera.rate - (time.monotonic() - started)
                            time.sleep(max(ahead, 0))
                except OSError:  # the client hung up: a cancelled download, or a player seeking
                    self.close_connection = True

            do_HEAD = do_GET

        return Handler


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    port = int(argv[0])
    options = argv[1:]

    def option(flag: str, count: int = 1):
        if flag in options:
            i = options.index(flag)
            return options[i + 1:i + 1 + count]
        return None

    cert = option("--https", 2)
    clip_file = option("--clip")
    camera = FakeCamera(
        (option("--password") or ["pw"])[0], port=port,
        certfile=cert[0] if cert else None, keyfile=cert[1] if cert else None,
        **({"clip_bytes": open(clip_file[0], "rb").read()} if clip_file else {}),
    )
    today = datetime.combine(date.today(), datetime.min.time())
    for day in (today, today - timedelta(days=1)):
        for hour, flags in ((8, "6D28808"), (12, "6D28C08"), (17, "6D28908")):  # motion, person, vehicle
            camera.add_clips(clip(day.replace(hour=hour), 60, "main", flags),
                             clip(day.replace(hour=hour), 60, "sub", flags))
    camera.start()
    print(f"fake Reolink API ready on {'https' if camera.https else 'http'}://127.0.0.1:{camera.port}"
          f"  (admin / {camera.password})", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
