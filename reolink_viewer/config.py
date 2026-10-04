"""Camera definitions, Reolink stream URL building and config persistence.

This module has no GTK/GStreamer dependency so it can be unit tested anywhere.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

PROTOCOLS = {
    "rtsp": "RTSP",
    "rtmp": "RTMP",
    "flv": "HTTP-FLV",
    "battery": "Battery camera (via neolink)",
    "custom": "Custom URL",
}
DEFAULT_PORTS = {"rtsp": 554, "rtmp": 1935, "flv": 80, "battery": 0, "custom": 0}
# Battery cameras have no RTSP server of their own: neolink wakes them over
# Reolink's own protocol and serves them here, to this computer only.
NEOLINK_PORT = 18554

STREAMS = {
    "main": "Main (high quality)",
    "sub": "Sub (low bandwidth)",
    "ext": "Balanced (over RTMP for RTSP cameras)",
}
# What the grid plays when several cameras share the screen ("" = each camera's
# own stream). A 4K main stream holds over 1 GB of GPU memory.
GRID_STREAMS = {
    "ext": "Balanced",
    "sub": "Sub (lowest quality)",
    "": "Each camera's own stream",
}
CODECS = {"h264": "H.264", "h265": "H.265 / HEVC"}


@dataclass
class Camera:
    name: str
    host: str = ""
    username: str = "admin"
    password: str = ""
    protocol: str = "rtsp"
    port: int = 554
    # 1-based channel. Standalone cameras use 1; NVRs / Home Hubs use 1..N.
    channel: int = 1
    stream: str = "sub"
    codec: str = "h264"
    audio: bool = False
    custom_url: str = ""
    uid: str = ""  # battery cameras: the UID from the Reolink app's Device Info
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @classmethod
    def from_dict(cls, data: dict) -> "Camera":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)

    def url(self, stream: str | None = None) -> str:
        return build_url(self, stream)

    def display_url(self, stream: str | None = None) -> str:
        return redact_url(self.url(stream))

    @property
    def on_demand(self) -> bool:
        """Streams only while someone watches: every minute awake costs battery."""
        return self.protocol == "battery"


def _host_port(cam: Camera) -> str:
    host = cam.host.strip()
    if ":" in host and not host.startswith("["):  # bare IPv6 literal
        host = f"[{host}]"
    port = cam.port or DEFAULT_PORTS.get(cam.protocol, 0)
    return f"{host}:{port}" if port else host


def _userinfo(cam: Camera) -> str:
    if not cam.username:
        return ""
    info = quote(cam.username, safe="")
    if cam.password:
        info += ":" + quote(cam.password, safe="")
    return info + "@"


# Reolink's RTMP server reads the login from the URL query without decoding it,
# so it has to be there literally. Besides letters, digits and - _ . ~ (never
# encoded), "!" works as is (checked 2026-10-04).
QUERY_LITERAL = "!"


def rtmp_safe(cam: Camera) -> bool:
    """Whether Reolink's RTMP / HTTP-FLV servers will accept the camera's login.

    Those take the username and password in the URL query, where any character
    that needs URL-encoding gets rejected (RTSP accepts them). Every rejection
    counts toward the camera's login lockout, and a locked-out camera refuses
    RTSP too.
    """
    return all(quote(text, safe=QUERY_LITERAL) == text for text in (cam.username, cam.password))


def resolve_stream(cam: Camera, stream: str) -> str:
    """The stream that can actually be played: RTSP cameras get the balanced
    stream over RTMP, which needs an RTMP-safe login, so otherwise sub.
    Battery cameras have no balanced stream."""
    if stream == "ext" and (
        cam.protocol == "battery" or (cam.protocol == "rtsp" and not rtmp_safe(cam))
    ):
        return "sub"
    return stream


def build_url(cam: Camera, stream: str | None = None) -> str:
    """Return the playable URL for a camera.

    RTSP:     rtsp://user:pass@host:554/h264Preview_01_main
    RTMP:     rtmp://host:1935/bcs/channel0_main.bcs?channel=0&stream=0&user=..&password=..
    HTTP-FLV: http://host:80/flv?port=1935&app=bcs&stream=channel0_main.bcs&user=..&password=..
    Battery:  rtsp://127.0.0.1:18554/<camera id>/mainStream (served by neolink)
    """
    stream = stream or cam.stream
    if cam.protocol == "battery":
        if not cam.uid.strip():
            raise ValueError(f"Camera '{cam.name}' has no UID")
        kind = "mainStream" if stream == "main" else "subStream"
        return f"rtsp://127.0.0.1:{NEOLINK_PORT}/{cam.id}/{kind}"
    if cam.protocol == "rtsp" and stream == "ext":
        # Reolink's RTSP server has no balanced stream, but its RTMP server does.
        cam = replace(cam, protocol="rtmp", port=DEFAULT_PORTS["rtmp"])
    proto = cam.protocol

    if proto == "custom":
        return cam.custom_url.strip()
    if not cam.host.strip():
        raise ValueError(f"Camera '{cam.name}' has no host/IP address")

    if proto == "rtsp":
        # Reolink RTSP only exposes main and sub.
        rtsp_stream = "main" if stream == "main" else "sub"
        path = f"{cam.codec}Preview_{cam.channel:02d}_{rtsp_stream}"
        return f"rtsp://{_userinfo(cam)}{_host_port(cam)}/{path}"

    ch = cam.channel - 1
    stream_name = f"channel{ch}_{stream}.bcs"
    creds = {"user": cam.username, "password": cam.password}

    if proto == "rtmp":
        query = urlencode(
            {"channel": ch, "stream": 1 if stream == "sub" else 0, **creds},
            safe=QUERY_LITERAL,
        )
        return f"rtmp://{_host_port(cam)}/bcs/{stream_name}?{query}"

    if proto == "flv":
        query = urlencode(
            {"port": 1935, "app": "bcs", "stream": stream_name, **creds}, safe=QUERY_LITERAL
        )
        return f"http://{_host_port(cam)}/flv?{query}"

    raise ValueError(f"Unknown protocol: {proto}")


def redact_url(url: str) -> str:
    """Hide passwords in a URL so it is safe to show in the UI or logs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:***@{hostport}" if ":" in userinfo else f"{user}@{hostport}"
    query = parts.query
    if query:
        pairs = []
        for item in query.split("&"):
            key, sep, _ = item.partition("=")
            pairs.append(f"{key}=***" if key.lower() in ("password", "pass", "pwd") else item)
        query = "&".join(pairs)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


@dataclass
class Settings:
    columns: int = 0  # 0 = automatic
    hd_when_maximized: bool = True
    grid_stream: str = "ext"  # a GRID_STREAMS key
    gpu_only: bool = True
    cameras: list[Camera] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        return cls(
            columns=int(data.get("columns", 0)),
            hd_when_maximized=bool(data.get("hd_when_maximized", True)),
            grid_stream=_grid_stream(data.get("grid_stream", "ext")),
            gpu_only=bool(data.get("gpu_only", True)),
            cameras=[Camera.from_dict(c) for c in data.get("cameras", [])],
        )

    def to_dict(self) -> dict:
        return {
            "columns": self.columns,
            "hd_when_maximized": self.hd_when_maximized,
            "grid_stream": self.grid_stream,
            "gpu_only": self.gpu_only,
            "cameras": [c.to_dict() for c in self.cameras],
        }


def _grid_stream(value) -> str:
    return value if value in GRID_STREAMS else "ext"


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "reolink-viewer" / "config.json"


def load_settings(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    with path.open(encoding="utf-8") as fh:
        return Settings.from_dict(json.load(fh))


def save_settings(settings: Settings, path: Path) -> None:
    """Atomically write the config, readable only by the owner (it holds passwords)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(settings.to_dict(), fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
