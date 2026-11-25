import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # test helpers such as fake_reolink_api


@pytest.fixture(scope="session")
def gtk():
    """Gtk with a usable display and GStreamer initialised, or skip.

    Window tests need a display: run them as `xvfb-run -a python3 -m pytest tests`
    on a machine without one.
    """
    try:
        from reolink_viewer.display import prepare_display

        prepare_display()  # before Gtk is imported
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst, Gtk
    except (ImportError, ValueError):
        pytest.skip("PyGObject with GTK 3 and GStreamer is not installed")
    if not Gtk.init_check()[0]:
        pytest.skip("no display (use xvfb-run)")
    Gst.init(None)
    return Gtk


@pytest.fixture
def run_loop(gtk):
    """run_loop(until, timeout=10): run the GTK main loop until until() is true.
    Returns whether it became true in time."""
    from gi.repository import GLib

    def run(until, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        context = GLib.MainContext.default()
        while not until():
            if time.monotonic() > deadline:
                return False
            if not context.iteration(False):
                time.sleep(0.005)
        return True

    return run
