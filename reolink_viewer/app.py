"""Main window: a grid of camera tiles with add/edit/remove, maximize and snapshots."""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, Gio, GLib, Gst, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, __version__  # noqa: E402
from .config import (  # noqa: E402
    Camera,
    Settings,
    default_config_path,
    load_settings,
    save_settings,
)
from .dialogs import CameraDialog  # noqa: E402
from .player import CameraTile  # noqa: E402

log = logging.getLogger(__name__)

CSS = b"""
.camera-bg { background-color: #000000; }
.camera-grid { background-color: #1a1a1a; }
.camera-label {
    background-color: rgba(0, 0, 0, 0.55);
    color: #ffffff;
    padding: 2px 8px;
    margin: 6px;
    border-radius: 4px;
    font-size: 90%;
}
.empty-state { font-size: 120%; }
"""


def snapshot_dir() -> Path:
    pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
    return Path(pictures or Path.home() / "Pictures") / "Reolink"


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: "ViewerApp"):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.settings = app.settings
        self.tiles: dict[str, CameraTile] = {}
        self.focused: CameraTile | None = None
        self.is_fullscreen = False
        self.set_default_size(1280, 760)

        header = Gtk.HeaderBar(show_close_button=True, title=APP_NAME)
        self.set_titlebar(header)

        add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        add_btn.set_tooltip_text("Add camera (Ctrl+N)")
        add_btn.connect("clicked", lambda *_: self.add_camera())
        header.pack_start(add_btn)

        fs_btn = Gtk.Button.new_from_icon_name("view-fullscreen-symbolic", Gtk.IconSize.BUTTON)
        fs_btn.set_tooltip_text("Fullscreen (F11)")
        fs_btn.connect("clicked", lambda *_: self.toggle_fullscreen())
        header.pack_end(fs_btn)

        menu_btn = Gtk.MenuButton()
        menu_btn.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu_btn.set_popup(self._build_main_menu())
        header.pack_end(menu_btn)

        self.stack = Gtk.Stack()
        self.add(self.stack)

        empty = Gtk.Label(
            label="No cameras yet.\nClick  +  to add a Reolink camera.",
            justify=Gtk.Justification.CENTER,
        )
        empty.get_style_context().add_class("empty-state")
        empty.get_style_context().add_class("dim-label")
        self.stack.add_named(empty, "empty")

        self.grid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True,
                             row_spacing=2, column_spacing=2)
        self.grid.get_style_context().add_class("camera-grid")
        self.stack.add_named(self.grid, "grid")

        self.connect("key-press-event", self._on_key)
        self.connect("window-state-event", self._on_window_state)
        self.connect("delete-event", self._on_delete)

        for cam in self.settings.cameras:
            self._create_tile(cam)
        self.show_all()
        self.relayout()
        for tile in self.tiles.values():
            tile.start()

    # --------------------------------------------------------------- layout

    def ordered_tiles(self) -> list[CameraTile]:
        return [self.tiles[c.id] for c in self.settings.cameras if c.id in self.tiles]

    def relayout(self) -> None:
        for child in self.grid.get_children():
            self.grid.remove(child)
        tiles = [self.focused] if self.focused else self.ordered_tiles()
        if not tiles:
            self.stack.set_visible_child_name("empty")
            return
        cols = self.settings.columns or math.ceil(math.sqrt(len(tiles)))
        cols = max(1, min(cols, len(tiles)))
        for i, tile in enumerate(tiles):
            self.grid.attach(tile, i % cols, i // cols, 1, 1)
            tile.show_all()
        self.stack.set_visible_child_name("grid")

    def maximize(self, tile: CameraTile) -> None:
        if self.focused is tile:
            return
        self.focused = tile
        for other in self.tiles.values():
            if other is not tile:
                other.stop()  # save bandwidth/CPU while hidden
        if self.settings.hd_when_maximized and tile.camera.protocol != "custom":
            tile.set_stream_override("main")
        if not tile.is_playing:
            tile.start()
        self.relayout()

    def restore(self) -> None:
        if not self.focused:
            return
        self.focused.set_stream_override(None)
        self.focused = None
        self.relayout()
        for tile in self.tiles.values():
            if not tile.is_playing:
                tile.start()

    def toggle_fullscreen(self) -> None:
        if self.is_fullscreen:
            self.unfullscreen()
        else:
            self.fullscreen()

    # ------------------------------------------------------------- cameras

    def _create_tile(self, cam: Camera) -> CameraTile:
        tile = CameraTile(cam)
        tile.connect("button-press-event", self._on_tile_click)
        self.tiles[cam.id] = tile
        return tile

    def add_camera(self) -> None:
        dlg = CameraDialog(self)
        if dlg.run() == Gtk.ResponseType.OK:
            cam = dlg.get_camera()
            self.settings.cameras.append(cam)
            self.app.save()
            tile = self._create_tile(cam)
            if self.focused:
                self.restore()
            else:
                self.relayout()
            tile.start()
        dlg.destroy()

    def edit_camera(self, tile: CameraTile) -> None:
        dlg = CameraDialog(self, tile.camera)
        if dlg.run() == Gtk.ResponseType.OK:
            cam = dlg.get_camera()
            idx = next(i for i, c in enumerate(self.settings.cameras) if c.id == cam.id)
            self.settings.cameras[idx] = cam
            self.app.save()
            tile.set_camera(cam)
        dlg.destroy()

    def remove_camera(self, tile: CameraTile) -> None:
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Remove camera “{tile.camera.name}”?",
        )
        answer = dlg.run()
        dlg.destroy()
        if answer != Gtk.ResponseType.YES:
            return
        if self.focused is tile:
            self.restore()
        self.settings.cameras = [c for c in self.settings.cameras if c.id != tile.camera.id]
        self.app.save()
        del self.tiles[tile.camera.id]
        tile.destroy()
        self.relayout()

    def move_camera(self, tile: CameraTile, delta: int) -> None:
        cams = self.settings.cameras
        i = next(i for i, c in enumerate(cams) if c.id == tile.camera.id)
        j = i + delta
        if 0 <= j < len(cams):
            cams[i], cams[j] = cams[j], cams[i]
            self.app.save()
            self.relayout()

    def take_snapshot(self, tile: CameraTile) -> None:
        data = tile.snapshot_png()
        if not data:
            self._notify("No frame available yet for a snapshot.")
            return
        folder = snapshot_dir()
        folder.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tile.camera.name)
        path = folder / f"{safe}_{datetime.now():%Y%m%d_%H%M%S}.png"
        path.write_bytes(data)
        log.info("snapshot saved to %s", path)
        self._notify(f"Snapshot saved to {path}")

    def _notify(self, text: str) -> None:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.OK, text=text)
        dlg.run()
        dlg.destroy()

    # ---------------------------------------------------------------- menus

    def _build_main_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        add = Gtk.MenuItem(label="Add camera…")
        add.connect("activate", lambda *_: self.add_camera())
        menu.append(add)

        layout_item = Gtk.MenuItem(label="Columns")
        layout_menu = Gtk.Menu()
        group = None
        for cols, label in [(0, "Automatic"), (1, "1"), (2, "2"), (3, "3"), (4, "4"), (5, "5")]:
            item = Gtk.RadioMenuItem(label=label, group=group)
            group = item
            item.set_active(self.settings.columns == cols)
            item.connect("toggled", self._on_columns, cols)
            layout_menu.append(item)
        layout_item.set_submenu(layout_menu)
        menu.append(layout_item)

        hd = Gtk.CheckMenuItem(label="Main stream when maximized", active=self.settings.hd_when_maximized)
        hd.connect("toggled", self._on_hd_toggled)
        menu.append(hd)

        reconnect = Gtk.MenuItem(label="Reconnect all")
        reconnect.connect("activate", lambda *_: [t.restart() for t in self.tiles.values() if t.is_playing])
        menu.append(reconnect)

        snaps = Gtk.MenuItem(label="Open snapshots folder")
        snaps.connect("activate", lambda *_: self._open_snapshots())
        menu.append(snaps)

        menu.append(Gtk.SeparatorMenuItem())
        about = Gtk.MenuItem(label="About")
        about.connect("activate", lambda *_: self._about())
        menu.append(about)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: self.app.quit())
        menu.append(quit_item)

        menu.show_all()
        return menu

    def _tile_menu(self, tile: CameraTile) -> Gtk.Menu:
        menu = Gtk.Menu()

        def item(label, cb, sensitive=True):
            mi = Gtk.MenuItem(label=label)
            mi.set_sensitive(sensitive)
            mi.connect("activate", lambda *_: cb())
            menu.append(mi)

        if self.focused is tile:
            item("Back to grid", self.restore)
        else:
            item("Maximize", lambda: self.maximize(tile))
        if tile.camera.protocol != "custom":
            other = "sub" if tile.active_stream == "main" else "main"
            item(f"Switch to {other} stream", lambda: tile.set_stream_override(other))
        item("Snapshot", lambda: self.take_snapshot(tile))
        item("Reconnect", tile.restart)
        menu.append(Gtk.SeparatorMenuItem())
        item("Move earlier", lambda: self.move_camera(tile, -1), self.focused is None)
        item("Move later", lambda: self.move_camera(tile, +1), self.focused is None)
        item("Edit…", lambda: self.edit_camera(tile))
        item("Remove", lambda: self.remove_camera(tile))
        menu.show_all()
        return menu

    def _open_snapshots(self) -> None:
        folder = snapshot_dir()
        folder.mkdir(parents=True, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)

    def _about(self) -> None:
        dlg = Gtk.AboutDialog(
            transient_for=self, modal=True,
            program_name=APP_NAME, version=__version__,
            comments="Live viewer for Reolink cameras and NVRs (RTSP / RTMP / HTTP-FLV).",
            logo_icon_name="camera-web",
        )
        dlg.run()
        dlg.destroy()

    # --------------------------------------------------------------- events

    def _on_columns(self, item: Gtk.RadioMenuItem, cols: int) -> None:
        if item.get_active():
            self.settings.columns = cols
            self.app.save()
            self.relayout()

    def _on_hd_toggled(self, item: Gtk.CheckMenuItem) -> None:
        self.settings.hd_when_maximized = item.get_active()
        self.app.save()

    def _on_tile_click(self, tile: CameraTile, event: Gdk.EventButton) -> bool:
        if event.type == Gdk.EventType._2BUTTON_PRESS and event.button == 1:
            if self.focused is tile:
                self.restore()
            else:
                self.maximize(tile)
            return True
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3:
            menu = self._tile_menu(tile)
            menu.attach_to_widget(tile, None)
            menu.popup_at_pointer(event)
            return True
        return False

    def _on_key(self, _w, event: Gdk.EventKey) -> bool:
        key = event.keyval
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        if key == Gdk.KEY_F11 or (key in (Gdk.KEY_f, Gdk.KEY_F) and not ctrl):
            self.toggle_fullscreen()
        elif key == Gdk.KEY_Escape:
            if self.focused:
                self.restore()
            elif self.is_fullscreen:
                self.unfullscreen()
        elif ctrl and key in (Gdk.KEY_n, Gdk.KEY_N):
            self.add_camera()
        elif ctrl and key in (Gdk.KEY_q, Gdk.KEY_Q):
            self.app.quit()
        elif Gdk.KEY_1 <= key <= Gdk.KEY_9 and not ctrl:
            tiles = self.ordered_tiles()
            idx = key - Gdk.KEY_1
            if idx < len(tiles):
                self.maximize(tiles[idx])
        elif key == Gdk.KEY_0 and not ctrl:
            self.restore()
        else:
            return False
        return True

    def _on_window_state(self, _w, event: Gdk.EventWindowState) -> bool:
        self.is_fullscreen = bool(event.new_window_state & Gdk.WindowState.FULLSCREEN)
        return False

    def _on_delete(self, *_):
        for tile in self.tiles.values():
            tile.dispose()
        return False


class ViewerApp(Gtk.Application):
    def __init__(self, config_path: Path, extra_urls: list[str]):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.config_path = config_path
        self.settings: Settings = Settings()
        self.persist = not extra_urls
        self.extra_urls = extra_urls
        self.window: MainWindow | None = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        if self.extra_urls:
            # Ad-hoc mode: just play the given URLs, don't touch the saved config.
            self.settings = Settings(cameras=[
                Camera(name=f"Stream {i + 1}", protocol="custom", custom_url=url)
                for i, url in enumerate(self.extra_urls)
            ])
        else:
            try:
                self.settings = load_settings(self.config_path)
            except (OSError, ValueError) as exc:
                log.error("could not read %s: %s", self.config_path, exc)
                self.settings = Settings()

    def do_activate(self):
        if self.window is None:
            self.window = MainWindow(self)
        self.window.present()

    def save(self) -> None:
        if not self.persist:
            return
        try:
            save_settings(self.settings, self.config_path)
        except OSError as exc:
            log.error("could not save %s: %s", self.config_path, exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reolink-viewer", description="Live viewer for Reolink cameras.")
    parser.add_argument("urls", nargs="*", help="play these stream URLs instead of the saved cameras")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="config file path")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    Gst.init(None)
    app = ViewerApp(args.config, args.urls)
    return app.run([sys.argv[0]])
