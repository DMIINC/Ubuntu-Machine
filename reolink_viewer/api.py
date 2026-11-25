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
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from .config import DEFAULT_PORTS, Camera, rtmp_safe

log = logging.getLogger(__name__)

TIMEOUT_S = 10
SEARCH_TIMEOUT_S = 25  # searching an SD card can take a while
LOGOUT_TIMEOUT_S = 2
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SEARCH_PAGES = 30
# A playback URL must outlive the clip it plays: renew the session when less
# than this is left (reolink_aio uses the same margin).
PLAYBACK_LEASE_MARGIN_S = 300
# HTTPS first: several cameras ship with plain HTTP turned off.
DEFAULT_API_PORTS = ((443, True), (80, False))

# Reolink's error details that mean "wrong login" or "locked out".
_CREDENTIAL_DETAILS = ("invalid user", "login failed", "password wrong", "locked")
_CREDENTIAL_CODES = (-7, -27, -28)  # login failed, invalid user, user not exist
_LOGIN_FIRST = -6  # "please login first": the token expired or was dropped
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


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a login must not be re-sent to wherever one points."""

    def redirect_request(self, *_args, **_kwargs):
        return None


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


def _opener(context: ssl.SSLContext | None) -> urllib.request.OpenerDirector:
    handlers: list = [_NoRedirects(), urllib.request.ProxyHandler({})]  # cameras are on the LAN
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


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
        self._logged_in_once = False

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
            self._logged_in_once = True
            log.info("[%s] logged in to the camera's API over %s", self.cam.name,
                     "HTTPS" if https else "HTTP")
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
        or the session ran out while the computer slept). Drop that token so the
        next playback_url() logs in afresh, which is safe: this login was accepted.
        Only the token in question is dropped, so two clips refused at once cause
        one new login. Returns whether it was still the current one."""
        with self._lock:
            if self._token and self._token in url_or_token:
                self._token, self._token_expires = "", 0.0
                return True
            return False

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

    # ------------------------------------------------------------- commands

    def _command(self, cmd: str, param: dict, action: int = 0, timeout: float = TIMEOUT_S) -> dict:
        """Send one command and return its whole answer item, raising for errors."""
        with self._lock:
            self.login()
            body = [{"cmd": cmd, "action": action, "param": param}]
            for attempt in (1, 2):
                try:
                    answer = self._post(self._base, self._https, cmd, body, self._token, timeout)
                except _NoAnswer as exc:
                    raise NotReachable(f"The camera stopped answering ({exc}).") from None
                item = answer[0] if answer and isinstance(answer[0], dict) else {}
                if item.get("code") == 0:
                    return item
                code, detail = _error_of(item)
                if code == _LOGIN_FIRST and attempt == 1:
                    # The session ran out early. This login was accepted before, so
                    # asking once more is safe; a refusal now is remembered as usual.
                    self._token, self._token_expires = "", 0.0
                    self.login()
                    continue
                if code in _NOT_SUPPORTED:
                    raise NotSupported(f"This camera doesn't support that ({detail or cmd}).")
                raise ApiError(f"The camera answered with an error: {detail or f'code {code}'}.")
            raise ApiError("The camera keeps asking to log in again.")

    def _post(self, base: str, https: bool, cmd: str, body: list, token: str,
              timeout: float, expect_json: bool = True) -> list:
        url = f"{base}/cgi-bin/api.cgi?{urlencode({'cmd': cmd, 'token': token})}"
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST", headers={"Content-Type": "application/json"})
        modes = (self._legacy_tls,) if not https or self._legacy_tls else (False, True)
        last: Exception | None = None
        for legacy in modes:
            opener = _opener(tls_context(legacy) if https else None)
            try:
                with opener.open(request, timeout=timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES)
            except urllib.error.HTTPError as exc:
                # A server error may come after the request was handled; a redirect
                # or "not found" means this isn't the camera's API.
                kind = _Unanswered if exc.code >= 500 else _NoAnswer
                raise kind(f"HTTP {exc.code}") from None
            except urllib.error.URLError as exc:
                # urllib wraps what fails while connecting and sending: nothing was
                # delivered, so old TLS (or the next port) may be tried.
                if isinstance(exc.reason, ssl.SSLError) and not legacy:
                    last = exc
                    continue
                raise _NoAnswer(_reason(exc)) from None
            except (OSError, http.client.HTTPException) as exc:
                # Not wrapped: it failed after the request went out (no answer in
                # time, the connection dropped, a broken TLS record, garbage back).
                raise _Unanswered(_reason(exc)) from None
            except ValueError as exc:  # a malformed URL: nothing was sent
                raise _NoAnswer(_reason(exc)) from None
            self._legacy_tls = legacy
            if not expect_json:
                return []
            text = raw.decode("utf-8", "replace")
            try:
                answer = json.loads(text)
            except ValueError:
                # A cut-off answer from the camera, or some other web server's page.
                kind = _Unanswered if text.lstrip()[:1] in ("[", "{") else _NoAnswer
                raise kind("not a Reolink API answer") from None
            if not isinstance(answer, list):
                raise _NoAnswer("not a Reolink API answer")
            return answer
        raise _NoAnswer(_reason(last) if last else "TLS handshake failed")

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
