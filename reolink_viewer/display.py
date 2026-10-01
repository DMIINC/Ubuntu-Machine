"""Display setup that must happen before GTK and GStreamer are initialised.

The app runs natively on Wayland. XWayland used to be forced on NVIDIA so the
OpenGL output could share GTK's GL context, but with fractional scaling it drew
the window shifted or 1.2x too large (spilling over other windows), so it's
no longer used unless GDK_BACKEND=x11 is set explicitly.
"""

import os


def prepare_display() -> None:
    """Must run before anything imports Gtk (PyGObject opens the display on import)."""
    backend = os.environ.get("GDK_BACKEND", "")
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" and not backend.startswith("x11"):
        # GTK's Wayland GL contexts are core profile. GStreamer's default
        # compatibility-profile context can't share with them (EGL_BAD_CONTEXT
        # on NVIDIA), which would leave the GPU output unusable.
        os.environ.setdefault("GST_GL_API", "opengl3")
