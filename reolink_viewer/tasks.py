"""Run slow work (network calls) off the GTK thread and bring the result back to it."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from gi.repository import GLib

log = logging.getLogger(__name__)


def run_async(work: Callable[[], object], done: Callable[[object], None] | None = None,
              failed: Callable[[Exception], None] | None = None) -> None:
    """Call work() on a worker thread, then done(result) or failed(exc) on the GTK thread."""

    def deliver(callback, value) -> bool:
        if callback is not None:
            callback(value)
        return False

    def runner() -> None:
        try:
            result = work()
        except Exception as exc:  # reported to the caller, on the GTK thread
            if failed is None:
                log.warning("background task failed: %s", exc)
            GLib.idle_add(deliver, failed, exc)
        else:
            GLib.idle_add(deliver, done, result)

    threading.Thread(target=runner, daemon=True).start()
