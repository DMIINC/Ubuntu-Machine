"""GTK-side helpers shared by the windows that talk to a camera's API (recordings, zoom and focus)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from . import api  # noqa: E402
from .config import Camera  # noqa: E402

# Logins the user has agreed to try although the password looks risky.
_confirmed: set[tuple[str, str, str]] = set()


def ensure_login_allowed(parent: Gtk.Window, cam: Camera) -> bool:
    """Whether to go ahead and log in to the camera's API.

    Says why not for cameras without one. For a password with symbols that
    Reolink's web services have refused before, asks first (once per login):
    a refused login counts toward the camera's lockout. If the camera had
    locked the login, asks whether to try once more.
    """
    problem = api.has_api(cam)
    if problem:
        _message(parent, Gtk.MessageType.INFO, problem)
        return False
    minutes = api.locked_since(cam)
    if minutes is not None:
        if not _ask(parent, f"Try logging in to “{cam.name}” again?",
                    f"{minutes:.0f} minutes ago the camera said this login was locked after too many "
                    "failed attempts. If the lock hasn't cleared yet, another try extends it.",
                    "Try once"):
            return False
        api.allow_after_lock(cam)
    session = api.session_for(cam)
    key = session.login_key()
    if session.logged_in or key in _confirmed or not api.login_is_risky(cam):
        return True
    if not _ask(parent, f"Log in to “{cam.name}” with this password?",
                "The password contains a symbol other than - _ . ~ or !  Reolink cameras have "
                "refused such passwords on their web interface, and each refused login counts "
                "toward the camera's lockout (a locked camera stops sending video for a while).\n\n"
                "The viewer will try once. If the camera refuses, it won't try again until you "
                "change the password.", "Try once"):
        return False
    _confirmed.add(key)
    return True


def _ask(parent: Gtk.Window, question: str, detail: str, go: str) -> bool:
    """A warning with Cancel (the default) and a button that goes ahead."""
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.WARNING,
                               text=question, secondary_text=detail)
    dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button(go, Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.CANCEL)
    answer = dialog.run()
    dialog.destroy()
    return answer == Gtk.ResponseType.OK


def _message(parent: Gtk.Window, kind: Gtk.MessageType, text: str) -> None:
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=kind,
                               buttons=Gtk.ButtonsType.OK, text=text)
    dialog.run()
    dialog.destroy()


def explain(exc: Exception) -> str:
    """Text for an error from a camera API call, fit to show in a window."""
    if isinstance(exc, api.ApiError):
        return str(exc)
    return f"Something went wrong: {exc}"
