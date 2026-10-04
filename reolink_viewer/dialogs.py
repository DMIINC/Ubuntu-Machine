"""Add / edit camera dialog."""

from __future__ import annotations

import dataclasses

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .config import CODECS, DEFAULT_PORTS, PROTOCOLS, STREAMS, Camera, rtmp_safe  # noqa: E402


def _combo(options: dict[str, str], active: str) -> Gtk.ComboBoxText:
    combo = Gtk.ComboBoxText()
    for key, label in options.items():
        combo.append(key, label)
    combo.set_active_id(active if active in options else next(iter(options)))
    return combo


class CameraDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, camera: Camera | None = None):
        title = "Edit camera" if camera else "Add camera"
        super().__init__(title=title, transient_for=parent, modal=True)
        self.set_default_size(460, -1)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self._ok = self.add_button("Save" if camera else "Add", Gtk.ResponseType.OK)
        self._ok.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)

        self._original = camera
        # One base for a new camera, so its id (and URL preview) stays the same.
        self._base = camera or Camera(name="")
        cam = self._base
        self._prev_protocol = cam.protocol

        grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin=16)
        self.get_content_area().add(grid)

        self.name = Gtk.Entry(text=cam.name, activates_default=True, placeholder_text="Front door")
        self.protocol = _combo(PROTOCOLS, cam.protocol)
        self.host = Gtk.Entry(text=cam.host, activates_default=True, placeholder_text="192.168.1.50")
        self.uid = Gtk.Entry(text=cam.uid, activates_default=True,
                             placeholder_text="Reolink app → camera → Settings → Device Info")
        self.port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port.set_value(cam.port or DEFAULT_PORTS.get(cam.protocol, 554))
        self.username = Gtk.Entry(text=cam.username, activates_default=True)
        self.password = Gtk.Entry(text=cam.password, visibility=False, activates_default=True)
        self.password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        show_pw = Gtk.CheckButton(label="Show")
        show_pw.connect("toggled", lambda b: self.password.set_visibility(b.get_active()))
        self.channel = Gtk.SpinButton.new_with_range(1, 64, 1)
        self.channel.set_value(cam.channel)
        self.channel.set_tooltip_text("Use 1 for a standalone camera; NVR / Home Hub channel otherwise")
        self.stream = _combo(STREAMS, cam.stream)
        self.codec = _combo(CODECS, cam.codec)
        self.codec.set_tooltip_text("RTSP only. Pick H.265 for cameras set to H.265 encoding (e.g. 4K models)")
        self.audio = Gtk.CheckButton(label="Play audio", active=cam.audio)
        self.custom_url = Gtk.Entry(
            text=cam.custom_url,
            activates_default=True,
            placeholder_text="rtsp://user:pass@192.168.1.50:554/h264Preview_01_main",
        )
        self.preview = Gtk.Label(xalign=0, selectable=True, wrap=True)
        self.preview.get_style_context().add_class("dim-label")
        self.hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=50)
        self.hint.get_style_context().add_class("dim-label")

        pw_box = Gtk.Box(spacing=6)
        pw_box.pack_start(self.password, True, True, 0)
        pw_box.pack_start(show_pw, False, False, 0)

        rows = [
            ("Name", self.name),
            ("Protocol", self.protocol),
            ("Host / IP", self.host),
            ("UID", self.uid),
            ("Port", self.port),
            ("Username", self.username),
            ("Password", pw_box),
            ("Channel", self.channel),
            ("Stream", self.stream),
            ("Codec", self.codec),
            ("Custom URL", self.custom_url),
            ("", self.audio),
            ("URL", self.preview),
            ("", self.hint),
        ]
        self._rows: dict[Gtk.Widget, Gtk.Label] = {}
        for i, (label, widget) in enumerate(rows):
            lbl = Gtk.Label(label=label, xalign=1)
            lbl.get_style_context().add_class("dim-label")
            widget.set_hexpand(True)
            grid.attach(lbl, 0, i, 1, 1)
            grid.attach(widget, 1, i, 1, 1)
            self._rows[widget] = lbl

        for w in (self.name, self.host, self.uid, self.username, self.password, self.custom_url):
            w.connect("changed", self._refresh)
        for w in (self.stream, self.codec):
            w.connect("changed", self._refresh)
        self.port.connect("value-changed", self._refresh)
        self.channel.connect("value-changed", self._refresh)
        self.protocol.connect("changed", self._on_protocol_changed)

        self.show_all()
        self._refresh()

    def _on_protocol_changed(self, *_):
        new = self.protocol.get_active_id()
        # Swap the port only if the user left it at the previous protocol's default.
        if int(self.port.get_value()) == DEFAULT_PORTS.get(self._prev_protocol) and DEFAULT_PORTS.get(new):
            self.port.set_value(DEFAULT_PORTS[new])
        self._prev_protocol = new
        self._refresh()

    def _refresh(self, *_):
        proto = self.protocol.get_active_id()
        custom = proto == "custom"
        battery = proto == "battery"
        shown = {
            self.host: not (custom or battery),
            self.port: not (custom or battery),
            self.uid: battery,
            self.username: not custom,
            self.password.get_parent(): not custom,
            self.channel: not (custom or battery),
            self.stream: not custom,
            self.codec: proto == "rtsp",
            self.custom_url: custom,
        }
        for widget, visible in shown.items():
            widget.set_visible(visible)
            self._rows[widget].set_visible(visible)

        cam = self.get_camera()
        try:
            url = cam.display_url() if (cam.host or custom or battery) else ""
        except ValueError:
            url = ""
        self.preview.set_text(url)
        if battery:
            hint = ("neolink finds the camera on your network by UID (Reolink's servers "
                    "aren't used). It sleeps until you double-click its tile.")
        elif proto == "rtsp" and cam.password and not rtmp_safe(cam):
            hint = ("The grid will play this camera's sub stream: the balanced stream "
                    "comes over RTMP, which only accepts passwords whose symbols "
                    "are - _ . ~ or !")
        else:
            hint = ""
        self.hint.set_text(hint)
        self.hint.set_visible(bool(hint))
        required = cam.custom_url if custom else cam.uid if battery else cam.host
        self._ok.set_sensitive(bool(cam.name.strip()) and bool(required.strip()))

    def get_camera(self) -> Camera:
        return dataclasses.replace(
            self._base,
            name=self.name.get_text().strip(),
            protocol=self.protocol.get_active_id(),
            host=self.host.get_text().strip(),
            uid="".join(self.uid.get_text().split()).upper(),
            port=int(self.port.get_value()),
            username=self.username.get_text().strip(),
            password=self.password.get_text(),
            channel=int(self.channel.get_value()),
            stream=self.stream.get_active_id(),
            codec=self.codec.get_active_id(),
            audio=self.audio.get_active(),
            custom_url=self.custom_url.get_text().strip(),
        )
