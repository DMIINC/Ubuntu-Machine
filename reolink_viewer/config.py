"""Camera definitions, Reolink stream URL building and config persistence.

This module has no GTK/GStreamer dependency so it can be unit tested anywhere.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

PROTOCOLS = {
    "rtsp": "RTSP",
    "rtmp": "RTMP",
    "flv": "HTTP-FLV",
    "custom": "Custom URL",
}
DEFAULT_PORTS = {"rtsp": 554, "rtmp": 1935, "flv": 80, "custom": 0}

STREAMS = {
    "main": "Main (high quality)",
    "sub": "Sub (low bandwidth)",
    "ext": "Ext / balanced (RTMP & FLV only)",
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


def build_url(cam: Camera, stream: str | None = None) -> str:
    """Return the playable URL for a camera.

    RTSP:     rtsp://user:pass@host:554/h264Preview_01_main
    RTMP:     rtmp://host:1935/bcs/channel0_main.bcs?channel=0&stream=0&user=..&password=..
    HTTP-FLV: http://host:80/flv?port=1935&app=bcs&stream=channel0_main.bcs&user=..&password=..
    """
    stream = stream or cam.stream
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
            {"channel": ch, "stream": 1 if stream == "sub" else 0, **creds}
        )
        return f"rtmp://{_host_port(cam)}/bcs/{stream_name}?{query}"

    if proto == "flv":
        query = urlencode({"port": 1935, "app": "bcs", "stream": stream_name, **creds})
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
    cameras: list[Camera] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        return cls(
            columns=int(data.get("columns", 0)),
            hd_when_maximized=bool(data.get("hd_when_maximized", True)),
            cameras=[Camera.from_dict(c) for c in data.get("cameras", [])],
        )

    def to_dict(self) -> dict:
        return {
            "columns": self.columns,
            "hd_when_maximized": self.hd_when_maximized,
            "cameras": [c.to_dict() for c in self.cameras],
        }


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
