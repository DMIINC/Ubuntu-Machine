import os

from reolink_viewer import display


def run(monkeypatch, env):
    for k in ("GDK_BACKEND", "XDG_SESSION_TYPE", "GST_GL_API"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    display.prepare_display()
    return os.environ.get("GDK_BACKEND"), os.environ.get("GST_GL_API")


def test_wayland_stays_native_with_core_gl(monkeypatch):
    assert run(monkeypatch, {"XDG_SESSION_TYPE": "wayland"}) == (None, "opengl3")


def test_x11_session_untouched(monkeypatch):
    assert run(monkeypatch, {"XDG_SESSION_TYPE": "x11"}) == (None, None)


def test_explicit_x11_backend_untouched(monkeypatch):
    env = {"XDG_SESSION_TYPE": "wayland", "GDK_BACKEND": "x11"}
    assert run(monkeypatch, env) == ("x11", None)


def test_explicit_gl_api_respected(monkeypatch):
    env = {"XDG_SESSION_TYPE": "wayland", "GST_GL_API": "gles2"}
    assert run(monkeypatch, env) == (None, "gles2")
