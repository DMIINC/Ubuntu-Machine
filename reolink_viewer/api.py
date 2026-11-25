"""A small client for the HTTP(S) API of Reolink cameras and NVRs.

Used for what the video streams don't carry: finding and playing back
recordings, and moving the zoom and focus motors. Standard library only, no
GTK, so it can be unit tested anywhere.

Every call blocks on the network: run them on a worker thread (tasks.run_async),
never on the GTK thread.

Logins are precious. A camera locks an account out after a few rejected logins,
and then refuses the video streams too, so:
  - the password only travels in the JSON body of the Login request, never in
    a URL (everything after that uses the token the camera hands back);
  - a rejected login is remembered and not repeated until the camera's login
    settings change;
  - a connection problem moves on to the next port, a rejection never does;
  - nothing here retries a rejected login by itself.

Request and response formats follow reolink_aio (MIT licence,
github.com/starkillerOG/reolink_aio), which Home Assistant uses against real
cameras.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import ssl
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from .config import DEFAULT_PORTS, Camera, rtmp_safe

log = logging.getLogger(__name__)

TIMEOUT_S = 10
USER_AGENT = "reolink-viewer"
# How a kept-alive connection the camera has closed meanwhile shows up.
_CLOSED = (ConnectionError, http.client.RemoteDisconnected, ssl.SSLEOFError, ssl.SSLZeroReturnError)
SEARCH_TIMEOUT_S = 25  # searching an SD card can take a while
LOGOUT_TIMEOUT_S = 2
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SEARCH_PAGES = 30
# A playback URL must outlive the clip it plays: renew the session when less
# than this is left (reolink_aio uses the same margin).
PLAYBACK_LEASE_MARGIN_S = 300
# HTTPS first: several cameras ship with plain HTTP turned off.
DEFAULT_API_PORTS = ((443, True), (80, False))
# Some firmware refuses a token it has only just handed out ("please login first";
# reolink_aio saw it on an RLC-520A). Such a token is asked with again after these
# pauses before the session is renewed.
FRESH_TOKEN_S = 15
FRESH_TOKEN_WAITS_S = (1.0, 4.0)  # openHAB's Reolink binding waits 5 s
# A camera that refused a renewed session as well isn't asked again sooner than this.
STUCK_RETRY_S = 60

# Reolink's error details that mean "wrong login" or "locked out".
_CREDENTIAL_DETAILS = ("invalid user", "login failed", "password wrong", "locked")
_CREDENTIAL_CODES = (-7, -27, -28)  # login failed, invalid user, user not exist
_LOGIN_FIRST = -6  # "please login first": the token expired or was dropped


def _says_login_first(code: int, detail: str) -> bool:
    return code == _LOGIN_FIRST or "login first" in detail.lower()


_STUCK = ("The camera accepted the login but then refused the request as not logged in "
          "(“please login first”), with a second new session too. Some Reolink firmware gets "
          "stuck like this until the camera restarts. Try again in a minute; if it keeps "
          "happening, restart the camera.")
_NOT_SUPPORTED = (-9, -26)  # not support, ability error


class ApiError(Exception):
    """A problem worth showing to the user; str(exc) is written for them."""


class LoginRejected(ApiError):
    """The camera refused the user name or password (or has locked the account)."""


class NotReachable(ApiError):
    """No HTTP(S) API answered at the camera's address."""


class NotSupported(ApiError):
    """This camera (or this kind of camera) doesn't have the feature."""


class _NoAnswer(Exception):
    """The request never got a usable answer from the camera's API (internal)."""


class _Unanswered(_NoAnswer):
    """The request reached the camera, but no usable answer came back (internal).

    For a Login that matters: the camera may have counted (and refused) it, so
    the password must not be offered again, not even at another port."""


def _now() -> float:
    """Seconds on a clock that keeps running while the computer sleeps: a camera's
    session runs out in real time, and monotonic() stops during suspend."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


@dataclass(frozen=True)
class Recording:
    """One recorded clip on the camera's SD card (or NVR disk). Times are the camera's local time."""

    start: datetime
    end: datetime
    size: int  # bytes, 0 if unknown
    stream: str  # "main" or "sub"
    name: str  # the file's path on the camera; "" on NVRs, which address clips by time
    triggers: tuple[str, ...] = ()  # e.g. ("Person", "Motion")
    # NVRs: the clip's PlaybackTime (UTC, YYYYmmddHHMMSS), what their playback
    # service is asked for; "" when the search result has none.
    playback_start: str = ""

    @property
    def duration(self) -> timedelta:
        return max(self.end - self.start, timedelta(0))

    @property
    def key(self) -> str:
        return self.name or f"{self.stream}-{self.start:%Y%m%d%H%M%S}"


@dataclass(frozen=True)
class ZoomFocus:
    zoom: int
    zoom_min: int
    zoom_max: int
    focus: int
    focus_min: int
    focus_max: int
    autofocus: bool | None = None  # None: the camera has no autofocus switch


def _reolink_time(moment: datetime) -> dict[str, int]:
    return {"year": moment.year, "mon": moment.month, "day": moment.day,
            "hour": moment.hour, "min": moment.minute, "sec": moment.second}


def _from_reolink_time(value: dict) -> datetime:
    return datetime(value["year"], value["mon"], value["day"],
                    value["hour"], value["min"], value["sec"])


# Bit positions (from the left) of what triggered a recording, in the hex field
# of a camera's file name: RecM01_20201222_075939_080140_6D28808_1A468F9.mp4
_TRIGGER_BITS = {17: "Person", 19: "Vehicle", 20: "Animal", 26: "Doorbell", 24: "Motion", 23: "Scheduled"}
# Home Hub file names (9 fields, layout version 2) shift the last three.
_TRIGGER_BITS_HUB_V2 = {17: "Person", 19: "Vehicle", 20: "Animal", 27: "Doorbell", 25: "Motion", 24: "Scheduled"}


def parse_triggers(file_name: str) -> tuple[str, ...]:
    """What set a recording off, decoded from its file name; () if it can't be told."""
    stem = file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    parts = stem.split("_")
    if not parts[0].startswith("Rec") or len(parts[0]) != 6:
        return ()
    if len(parts) == 6:
        hex_flags, layout = parts[4], _TRIGGER_BITS
    elif len(parts) == 7:
        hex_flags, layout = parts[5], _TRIGGER_BITS
    elif len(parts) == 9:
        hex_flags = parts[7]
        layout = _TRIGGER_BITS_HUB_V2 if parts[0][4:6] == "02" else _TRIGGER_BITS
    else:
        return ()
    try:
        bits = bin(int(hex_flags, 16))[2:].zfill(len(hex_flags) * 4)
    except ValueError:
        return ()
    return tuple(label for bit, label in layout.items() if bit < len(bits) and bits[bit] == "1")


def _parse_recording(data: dict, stream: str) -> Recording | None:
    try:
        start = _from_reolink_time(data["StartTime"])
        end = _from_reolink_time(data["EndTime"])
    except (KeyError, TypeError, ValueError):
        return None
    name = str(data.get("name") or "")
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    try:
        playback_start = f"{_from_reolink_time(data['PlaybackTime']):%Y%m%d%H%M%S}"
    except (KeyError, TypeError, ValueError):
        playback_start = ""
    return Recording(start, end, size, str(data.get("type") or stream), name, parse_triggers(name),
                     playback_start)


def tls_context(legacy: bool = False) -> ssl.SSLContext:
    """TLS settings for talking to a camera. Cameras present self-signed
    certificates, so there is nothing to verify them against (the same settings
    as reolink_aio). "legacy" also accepts the old TLS versions and small keys
    of older firmware."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    if legacy:
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        context.set_ciphers("DEFAULT:@SECLEVEL=0")
    else:
        context.set_ciphers("DEFAULT")
    return context


def _error_of(item: dict) -> tuple[int, str]:
    error = item.get("error") or {}
    try:
        code = int(error.get("rspCode", item.get("code", -1)))
    except (TypeError, ValueError):
        code = -1
    return code, str(error.get("detail") or "")


def _is_credential_error(code: int, detail: str) -> bool:
    detail = detail.lower()
    return code in _CREDENTIAL_CODES or any(text in detail for text in _CREDENTIAL_DETAILS)


# Logins a camera has refused: (host, user, password) -> what to tell the user.
# Not tried again until one of the three changes.
_rejected: dict[tuple[str, str, str], str] = {}
# Logins the camera answered with "locked" -> when. A lock passes, so the user
# may ask for one more try (apiui does), but nothing tries again by itself.
_locked: dict[tuple[str, str, str], float] = {}
# One login at a time per (host, user, password), across sessions: two entries
# for one device (NVR channels, a dual-lens camera) must not both send a login
# that is about to be refused.
_login_locks: dict[tuple[str, str, str], threading.Lock] = {}
_login_locks_guard = threading.Lock()


def _login_lock(key: tuple[str, str, str]) -> threading.Lock:
    with _login_locks_guard:
        return _login_locks.setdefault(key, threading.Lock())


def locked_since(cam: Camera) -> float | None:
    """Minutes since the camera said this login is locked, or None."""
    at = _locked.get((cam.host.strip(), cam.username, cam.password))
    return None if at is None else (_now() - at) / 60


def allow_after_lock(cam: Camera) -> None:
    """The user asked to try a locked login once more."""
    _locked.pop((cam.host.strip(), cam.username, cam.password), None)


_LOCKED = ("The camera has locked this login after too many failed attempts. Wait for the lock "
           "to clear (or restart the camera), then open this window again to try once more.")


def login_is_risky(cam: Camera) -> bool:
    """Whether to ask before the first login: Reolink's web services have refused
    passwords containing symbols other than - _ . ~ ! (seen on this project's
    cameras over RTMP and the HTTP API), and each refusal counts toward a lockout."""
    return not rtmp_safe(cam)


def has_api(cam: Camera) -> str | None:
    """Why this camera has no HTTP API to talk to, or None if it should have one."""
    if cam.protocol == "battery":
        return "Battery cameras have no web interface: only the Reolink app can reach their recordings and settings."
    if cam.protocol == "custom" or not cam.host.strip():
        return "This needs the camera's address (it's set up with a custom URL)."
    return None


class CameraApi:
    """One camera's API session. Methods block; call them from a worker thread."""

    def __init__(self, cam: Camera, ports: tuple[tuple[int, bool], ...] | None = None):
        self.cam = cam
        if ports is None:
            port = int(getattr(cam, "api_port", 0) or 0)
            ports = ((port, True), (port, False)) if port else DEFAULT_API_PORTS
        self._ports = ports
        self._lock = threading.RLock()
        self._base = ""  # e.g. https://192.168.1.50:443
        self._https = False
        self._legacy_tls = False
        self._token = ""
        self._token_expires = 0.0
        self._login_at = 0.0  # when the camera last handed out a token
        self._stuck_at = 0.0  # when a renewed session was refused too
        # The kept-alive connection the session's requests go over (http.client
        # neither follows redirects nor uses a proxy: cameras are on the LAN).
        self._conn: http.client.HTTPConnection | None = None
        self._conn_key: tuple[str, bool] | None = None  # (base, old TLS) it was made for
        self._conn_number = 0  # connections opened so far, for the log
        self._conn_uses = 0  # requests sent on the current one

    # ------------------------------------------------------------- identity

    @property
    def channel(self) -> int:
        return max(int(self.cam.channel), 1) - 1  # the API counts from 0

    def login_key(self) -> tuple[str, str, str]:
        return (self.cam.host.strip(), self.cam.username, self.cam.password)

    @property
    def logged_in(self) -> bool:
        return bool(self._token) and _now() < self._token_expires

    # ---------------------------------------------------------------- login

    def login(self) -> None:
        """Log in unless a session is still valid. At most one login request reaches
        the camera's API, and none if this login was refused before."""
        with self._lock:
            if self.logged_in:
                return
            problem = has_api(self.cam)
            if problem:
                raise NotSupported(problem)
            key = self.login_key()
            with _login_lock(key):
                self._login(key)

    def _login(self, key: tuple[str, str, str]) -> None:
        if key in _rejected:
            raise LoginRejected(_rejected[key])
        if key in _locked:
            raise LoginRejected(_LOCKED)
        body = [{"cmd": "Login", "action": 0, "param": {
            "User": {"userName": self.cam.username, "password": self.cam.password}}}]
        tried = []
        for port, https in self._ports:
            base = f"{'https' if https else 'http'}://{_host(self.cam)}:{port}"
            try:
                answer = self._post(base, https, "Login", body, "null", TIMEOUT_S)
            except _Unanswered as exc:
                # The camera got the login and may have counted it: the same
                # password is not offered at another port.
                log.warning("[%s] API login got no usable answer: %s", self.cam.name, exc)
                raise ApiError(
                    "The camera got the login but didn't answer properly. Not trying again "
                    "now; try once more in a minute.") from None
            except _NoAnswer as exc:
                tried.append(f"{'HTTPS' if https else 'HTTP'} port {port}: {exc}")
                continue
            # The camera's API answered. Whatever it said, the same password
            # is not offered at another port.
            self._finish_login(base, https, answer, key)
            return
        log.info("[%s] no HTTP(S) API reachable: %s", self.cam.name, "; ".join(tried))
        raise NotReachable(
            "Couldn't reach the camera's web interface. In the Reolink app, check "
            "Settings → Network → Advanced → Server Settings: HTTPS (or HTTP) must be on."
        )

    def _finish_login(self, base: str, https: bool, answer: list, key: tuple[str, str, str]) -> None:
        item = answer[0] if answer and isinstance(answer[0], dict) else {}
        token = ((item.get("value") or {}).get("Token") or {}) if item.get("code") == 0 else {}
        if token.get("name"):
            try:
                lease = float(token.get("leaseTime", 3600))
            except (TypeError, ValueError):
                lease = 3600.0
            self._base, self._https = base, https
            self._token = str(token["name"])
            # Renew a minute early rather than have a command bounce.
            self._token_expires = _now() + max(lease - 60, 30)
            self._login_at = _now()
            log.info("[%s] logged in to the camera's API over %s (session of %.0f s)", self.cam.name,
                     "HTTPS" if https else "HTTP", lease)
            return
        code, detail = _error_of(item)
        log.warning("[%s] API login refused: %s (%s)", self.cam.name, detail or "no detail", code)
        if _is_credential_error(code, detail):
            if "locked" in detail.lower():
                # Says nothing about the password; a lock passes after a while.
                _locked[key] = _now()
                raise LoginRejected(_LOCKED)
            message = ("The camera rejected the user name or password. It won't be tried "
                       "again until you change them (right-click → Edit…).")
            _rejected[key] = message
            raise LoginRejected(message)
        if code == -5:
            raise ApiError("The camera has too many open sessions right now. Try again in a few minutes.")
        raise ApiError(f"The camera refused the login ({detail or f'code {code}'}).")

    def forget_token(self, url_or_token: str) -> bool:
        """The camera refused a playback URL with "please login first" (it restarted,
        or the session ran out while the computer slept). Give that session back
        and drop it, so the next playback_url() logs in afresh, which is safe: this
        login was accepted. Only the token in question is dropped, so two clips
        refused at once cause one new login. Returns whether it was still the current one."""
        with self._lock:
            if self._token and self._token in url_or_token:
                self.logout()
                return True
            return False

    def token_is_fresh(self, url_or_token: str) -> bool:
        """Whether this is the current token and the camera handed it out only just now."""
        with self._lock:
            return (bool(self._token) and self._token in url_or_token
                    and _now() - self._login_at < FRESH_TOKEN_S)

    def logout(self, timeout: float = LOGOUT_TIMEOUT_S) -> None:
        """Give the session back (cameras allow only a few). Never raises."""
        with self._lock:
            # Without a token, Logout breaks later commands on some firmware.
            if not self._token:
                return
            token, self._token, self._token_expires = self._token, "", 0.0
            try:
                self._post(self._base, self._https, "Logout",
                           [{"cmd": "Logout", "action": 0, "param": {}}], token, timeout,
                           expect_json=False)
            except _NoAnswer:
                pass
            finally:
                self._close_connection()

    # ------------------------------------------------------------- commands

    def _command(self, cmd: str, param: dict, action: int = 0, timeout: float = TIMEOUT_S) -> dict:
        """Send one command and return its whole answer item, raising for errors.

        An answer of "please login first" means the camera dropped the session (it
        restarted, or the session ran out while the computer slept), or that it
        doesn't take a token it has only just handed out yet. A fresh token is asked
        with again after a pause or two; then the session is given back and renewed
        once, which is safe: the camera has accepted this login. Nothing more.
        """
        with self._lock:
            if self._stuck_at and _now() - self._stuck_at < STUCK_RETRY_S:
                raise ApiError(_STUCK)
            self.login()
            body = [{"cmd": cmd, "action": action, "param": param}]
            renewed = False
            pauses = list(FRESH_TOKEN_WAITS_S)
            while True:
                try:
                    answer = self._post(self._base, self._https, cmd, body, self._token, timeout)
                except _NoAnswer as exc:
                    raise NotReachable(f"The camera stopped answering ({exc}).") from None
                item = answer[0] if answer and isinstance(answer[0], dict) else {}
                if item.get("code") == 0:
                    if pauses != list(FRESH_TOKEN_WAITS_S) or renewed:
                        log.info("[%s] the camera accepted the session for %s %.1f s after the login",
                                 self.cam.name, cmd, _now() - self._login_at)
                    self._stuck_at = 0.0
                    return item
                code, detail = _error_of(item)
                if not _says_login_first(code, detail):
                    if code in _NOT_SUPPORTED:
                        raise NotSupported(f"This camera doesn't support that ({detail or cmd}).")
                    raise ApiError(f"The camera answered with an error: {detail or f'code {code}'}.")
                age = _now() - self._login_at
                # The answer's cmd is "Unknown" if the camera couldn't read the request.
                said = f"“{detail}” ({code}, cmd {item.get('cmd', '?')})"
                if age < FRESH_TOKEN_S and pauses:
                    pause = pauses.pop(0)
                    log.warning("[%s] the camera refused %s with %s %.1f s after accepting the "
                                "login: asking again in %.0f s", self.cam.name, cmd, said, age, pause)
                    time.sleep(pause)
                    continue
                if renewed:
                    self._stuck_at = _now()
                    log.warning("[%s] the camera refused %s with %s with a renewed session too "
                                "(%.1f s after that login, over %s)", self.cam.name, cmd, said, age, self._base)
                    raise ApiError(_STUCK)
                renewed = True
                log.warning("[%s] the camera refused %s with %s %.0f s after the login: "
                            "renewing the session", self.cam.name, cmd, said, age)
                self.logout()  # with its token: cameras allow only a few sessions
                self.login()
                pauses = list(FRESH_TOKEN_WAITS_S)

    def _post(self, base: str, https: bool, cmd: str, body: list, token: str,
              timeout: float, expect_json: bool = True) -> list:
        """Send one request on the session's kept-alive connection and return the answer.

        A Login always goes on a new connection, and the commands after it on the
        same one, as browsers and reolink_aio's HTTP client do: on new Reolink
        firmware a token may only work on the connection it was handed out on.
        Raises _NoAnswer when the request can't have reached the camera's API
        (so another port or old TLS may be tried), _Unanswered when it may have.
        """
        path = f"/cgi-bin/api.cgi?{urlencode({'cmd': cmd, 'token': token})}"
        data = json.dumps(body).encode("utf-8")
        modes = (self._legacy_tls,) if not https or self._legacy_tls else (False, True)
        last: Exception | None = None
        for legacy in modes:
            began = time.monotonic()
            try:
                conn, reused = self._connection(base, https, legacy, timeout, fresh=cmd == "Login")
            except ssl.SSLError as exc:
                if legacy:
                    raise _NoAnswer(_reason(exc)) from None
                last = exc  # the handshake failed: nothing was sent, old TLS may work
                continue
            except (OSError, http.client.HTTPException, ValueError) as exc:
                raise _NoAnswer(_reason(exc)) from None
            try:
                status, kind, raw = self._exchange(conn, path, data)
            except _CLOSED as exc:
                self._close_connection()
                if not reused:
                    raise _Unanswered(_reason(exc)) from None
                # The camera closed the connection while it was idle. Once more on a
                # new one (a command, never a Login: those always start a connection).
                log.debug("[%s] API connection %d was closed by the camera (%s): opening another",
                          self.cam.name, self._conn_number, _reason(exc))
                try:
                    conn, reused = self._connection(base, https, legacy, timeout, fresh=True)
                except (OSError, http.client.HTTPException, ValueError) as again:
                    raise _NoAnswer(_reason(again)) from None
                try:
                    status, kind, raw = self._exchange(conn, path, data)
                except (OSError, http.client.HTTPException) as again:
                    self._close_connection()
                    raise _Unanswered(_reason(again)) from None
            except (OSError, http.client.HTTPException) as exc:
                # It failed after the request went out (no answer in time, a broken
                # TLS record, garbage back): the camera may have handled it.
                self._close_connection()
                raise _Unanswered(_reason(exc)) from None
            self._legacy_tls = legacy
            took = (time.monotonic() - began) * 1000
            where = f"{base} (connection {self._conn_number}, request {self._conn_uses})"
            if status != 200:
                log.debug("[%s] API %s at %s: HTTP %s, %.0f ms", self.cam.name, cmd, where, status, took)
                # A server error may come after the request was handled; a redirect
                # (never followed) or "not found" means this isn't the camera's API.
                raise (_Unanswered if status >= 500 else _NoAnswer)(f"HTTP {status}")
            if not expect_json:
                log.debug("[%s] API %s at %s: HTTP %s, %.0f ms", self.cam.name, cmd, where, status, took)
                return []
            text = raw.decode("utf-8", "replace")
            try:
                answer = json.loads(text)
            except ValueError:
                answer = None
            if not isinstance(answer, list):
                log.debug("[%s] API %s at %s: HTTP %s (%s), %d bytes that aren't an API answer, %.0f ms",
                          self.cam.name, cmd, where, status, kind, len(raw), took)
                # A cut-off answer from the camera, or some other web server's page.
                fault = _Unanswered if answer is None and text.lstrip()[:1] in ("[", "{") else _NoAnswer
                raise fault("not a Reolink API answer")
            log.debug("[%s] API %s at %s: HTTP %s (%s), %s, %.0f ms", self.cam.name, cmd, where, status, kind,
                      _summary(cmd, answer), took)
            return answer
        raise _NoAnswer(_reason(last) if last else "TLS handshake failed")

    def _connection(self, base: str, https: bool, legacy: bool, timeout: float,
                    fresh: bool) -> tuple[http.client.HTTPConnection, bool]:
        """(the connection to use, whether it served a request before). Connects a
        new one if asked to or if there is none for this address and TLS mode."""
        key = (base, legacy)
        conn = self._conn
        if not fresh and conn is not None and conn.sock is not None and self._conn_key == key:
            conn.sock.settimeout(timeout)
            return conn, True
        self._close_connection()
        address = urlsplit(base)
        if https:
            conn = http.client.HTTPSConnection(address.hostname, address.port, timeout=timeout,
                                               context=tls_context(legacy))
        else:
            conn = http.client.HTTPConnection(address.hostname, address.port, timeout=timeout)
        conn.connect()  # TCP and TLS: what fails here never reached the API
        self._conn, self._conn_key = conn, key
        self._conn_number += 1
        self._conn_uses = 0
        return conn, False

    def _exchange(self, conn: http.client.HTTPConnection, path: str, data: bytes) -> tuple[int, str, bytes]:
        """POST on the connection and read the whole answer (so the connection can
        serve the next request): (HTTP status, content type, body)."""
        self._conn_uses += 1
        conn.request("POST", path, body=data, headers={
            "Content-Type": "application/json", "User-Agent": USER_AGENT, "Connection": "keep-alive"})
        response = conn.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if response.will_close or len(raw) > MAX_RESPONSE_BYTES or not response.isclosed():
            self._close_connection()  # the camera ends it, or the rest wasn't read
        return response.status, response.getheader("Content-Type", "?"), raw[:MAX_RESPONSE_BYTES]

    def _close_connection(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ----------------------------------------------------------- recordings

    def recording_days(self, year: int, month: int, stream: str = "main") -> list[int]:
        """The days of a month that have recordings."""
        start = datetime(year, month, 1)
        # Any window inside a month returns that month's table.
        item = self._command("Search", {"Search": {
            "channel": self.channel, "onlyStatus": 1, "streamType": stream,
            "StartTime": _reolink_time(start),
            "EndTime": _reolink_time(start + timedelta(minutes=5)),
        }}, timeout=SEARCH_TIMEOUT_S)
        days: set[int] = set()
        for status in ((item.get("value") or {}).get("SearchResult") or {}).get("Status") or []:
            if status.get("year") == year and status.get("mon") == month:
                days.update(day for day, flag in enumerate(str(status.get("table", "")), 1)
                            if flag == "1")
        return sorted(days)

    def recordings(self, day: date, stream: str = "main") -> list[Recording]:
        """All clips recorded on one day, oldest first."""
        start = datetime(day.year, day.month, day.day)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        found: dict[str, Recording] = {}
        cursor = start
        for _ in range(MAX_SEARCH_PAGES):
            item = self._command("Search", {"Search": {
                "channel": self.channel, "onlyStatus": 0, "streamType": stream,
                "StartTime": _reolink_time(cursor), "EndTime": _reolink_time(end),
            }}, timeout=SEARCH_TIMEOUT_S)
            files = ((item.get("value") or {}).get("SearchResult") or {}).get("File") or []
            fresh = [rec for rec in (_parse_recording(f, stream) for f in files)
                     if rec is not None and rec.key not in found]
            if not fresh:
                break
            for rec in fresh:
                found[rec.key] = rec
            # Firmware may cap how many files one search returns: ask again from
            # the latest one found until nothing new comes back.
            latest = max(rec.end for rec in fresh)
            if latest <= cursor or latest >= end:
                break
            cursor = latest
        return sorted(found.values(), key=lambda rec: rec.start)

    def playback_url(self, rec: Recording) -> str:
        """A URL that plays the clip. Carries the session token (or, for NVRs,
        the login), so treat it like a password: redact it before logging."""
        with self._lock:
            if self.logged_in and self._token_expires - _now() < PLAYBACK_LEASE_MARGIN_S - 60:
                # The clip may play for minutes: start it on a fresh session.
                self.logout()
            self.login()
            if rec.name:
                # The camera sends the file as MP4.
                match = re.match(r".*Rec\w{3}(?:_|_DST)(\d{8})_(\d{6})_", rec.name)
                start_id = f"{match[1]}{match[2]}" if match else f"{rec.start:%Y%m%d%H%M%S}"
                source = rec.name.replace(" ", "%20")
                return (f"{self._base}/cgi-bin/api.cgi?cmd=Playback&source={source}"
                        f"&output=playback_{start_id}.mp4&start={start_id}&token={self._token}")
            # NVRs address clips by start time and stream them as FLV. That service
            # wants the login in the URL, unencoded, so only URL-safe logins work.
            if not rtmp_safe(self.cam):
                raise NotSupported(
                    "Playing recordings from an NVR needs a password whose only symbols are - _ . ~ or !")
            stream_type = 1 if rec.stream == "sub" else 0
            return (f"{self._base}/flv?port={DEFAULT_PORTS['rtmp']}&app=bcs&stream=playback.bcs"
                    f"&channel={self.channel}&type={stream_type}"
                    f"&start={rec.playback_start or f'{rec.start:%Y%m%d%H%M%S}'}"
                    f"&seek=0&user={self.cam.username}&password={self.cam.password}")

    @property
    def uses_https(self) -> bool:
        return self._https

    @property
    def legacy_tls(self) -> bool:
        """The camera only managed a TLS handshake with the old settings."""
        return self._https and self._legacy_tls

    # ------------------------------------------------------- zoom and focus

    def zoom_focus(self) -> ZoomFocus:
        """Current zoom and focus positions with their ranges."""
        item = self._command("GetZoomFocus", {"channel": self.channel}, action=1)
        try:
            value = item["value"]["ZoomFocus"]
            limits = item["range"]["ZoomFocus"]
            state = ZoomFocus(
                zoom=int(value["zoom"]["pos"]),
                zoom_min=int(limits["zoom"]["pos"]["min"]),
                zoom_max=int(limits["zoom"]["pos"]["max"]),
                focus=int(value["focus"]["pos"]),
                focus_min=int(limits["focus"]["pos"]["min"]),
                focus_max=int(limits["focus"]["pos"]["max"]),
            )
        except (KeyError, TypeError, ValueError):
            raise NotSupported("This camera has no motorised zoom or focus.") from None
        if state.zoom_max <= state.zoom_min:
            raise NotSupported("This camera has no motorised zoom or focus.")
        return replace(state, autofocus=self._autofocus())

    def _autofocus(self) -> bool | None:
        try:
            item = self._command("GetAutoFocus", {"channel": self.channel}, action=1)
            return int(item["value"]["AutoFocus"]["disable"]) == 0
        except (LoginRejected, NotReachable):
            raise
        except (ApiError, KeyError, TypeError, ValueError):
            return None  # no autofocus switch on this model

    def set_zoom(self, position: int) -> None:
        self._start_zoom_focus("ZoomPos", position)

    def set_focus(self, position: int) -> None:
        self._start_zoom_focus("FocusPos", position)

    def _start_zoom_focus(self, op: str, position: int) -> None:
        item = self._command("StartZoomFocus", {
            "ZoomFocus": {"channel": self.channel, "op": op, "pos": int(position)}})
        _check_setting(item)

    def set_autofocus(self, enabled: bool) -> None:
        item = self._command("SetAutoFocus", {
            "AutoFocus": {"disable": 0 if enabled else 1, "channel": self.channel}})
        _check_setting(item)


def _summary(cmd: str, answer: list) -> str:
    """What an answer said, for the log. Never a value: a Login answer holds the token."""
    item = answer[0] if answer and isinstance(answer[0], dict) else {}
    if item.get("code") == 0:
        if cmd == "Login":
            token = (item.get("value") or {}).get("Token") or {}
            return f"code 0, token {'given' if token.get('name') else 'missing'}, lease {token.get('leaseTime', '?')}"
        return "code 0"
    code, detail = _error_of(item)
    return f"error {code} ({detail})" if detail else f"error {code}"


def _check_setting(item: dict) -> None:
    code = (item.get("value") or {}).get("rspCode", 200)
    if code != 200:
        raise ApiError(f"The camera didn't accept the change (code {code}).")


def _host(cam: Camera) -> str:
    host = cam.host.strip()
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _reason(exc: Exception | None) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, TimeoutError) or "timed out" in str(reason):
        return "no answer"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    return str(reason) or type(reason).__name__


# One session per camera for the whole run: fewer logins, and cameras only allow a few sessions.
_sessions: dict[str, CameraApi] = {}
_sessions_lock = threading.Lock()


def session_for(cam: Camera) -> CameraApi:
    """The camera's shared API session, replaced if its address or login was edited."""
    with _sessions_lock:
        api = _sessions.get(cam.id)
        if api is None or _identity(api.cam) != _identity(cam):
            if api is not None:
                threading.Thread(target=api.logout, daemon=True).start()
            api = _sessions[cam.id] = CameraApi(cam)
        else:
            api.cam = cam  # e.g. a renamed camera
        return api


def _identity(cam: Camera) -> tuple:
    return (cam.host.strip(), cam.username, cam.password, cam.channel,
            getattr(cam, "api_port", 0), cam.protocol)


def logout_all(timeout: float = LOGOUT_TIMEOUT_S) -> None:
    """Log out of every camera at once, waiting at most about `timeout` seconds in total."""
    with _sessions_lock:
        sessions = [api for api in _sessions.values() if api.logged_in]
        _sessions.clear()
    threads = [threading.Thread(target=api.logout, args=(timeout,), daemon=True) for api in sessions]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout + 0.5
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
