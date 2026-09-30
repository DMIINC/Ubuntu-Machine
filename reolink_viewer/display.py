"""Pick the GTK display backend before GTK is imported.

On NVIDIA + Wayland, GStreamer's OpenGL output can't share a context with GTK 3,
but it works through XWayland, where decode -> GL -> screen stays on the GPU.
"""

import os


def _nvidia_driver_loaded() -> bool:
    return os.path.exists("/proc/driver/nvidia/version")


def choose_gdk_backend() -> None:
    """Must run before anything imports Gtk (PyGObject opens the display on import)."""
    if os.environ.get("GDK_BACKEND") or os.environ.get("REOLINK_VIEWER_NATIVE_WAYLAND") == "1":
        return  # the user chose explicitly
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" and _nvidia_driver_loaded():
        os.environ["GDK_BACKEND"] = "x11"
