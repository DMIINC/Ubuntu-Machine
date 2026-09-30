import os

from reolink_viewer import display


def run(monkeypatch, env, nvidia):
    for k in ("GDK_BACKEND", "XDG_SESSION_TYPE", "REOLINK_VIEWER_NATIVE_WAYLAND"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(display, "_nvidia_driver_loaded", lambda: nvidia)
    display.choose_gdk_backend()
    return os.environ.get("GDK_BACKEND")


def test_nvidia_wayland_uses_x11(monkeypatch):
    assert run(monkeypatch, {"XDG_SESSION_TYPE": "wayland"}, True) == "x11"


def test_other_gpus_stay_on_wayland(monkeypatch):
    assert run(monkeypatch, {"XDG_SESSION_TYPE": "wayland"}, False) is None


def test_x11_session_untouched(monkeypatch):
    assert run(monkeypatch, {"XDG_SESSION_TYPE": "x11"}, True) is None


def test_explicit_choice_respected(monkeypatch):
    env = {"XDG_SESSION_TYPE": "wayland", "GDK_BACKEND": "wayland"}
    assert run(monkeypatch, env, True) == "wayland"
    env = {"XDG_SESSION_TYPE": "wayland", "REOLINK_VIEWER_NATIVE_WAYLAND": "1"}
    assert run(monkeypatch, env, True) is None
