from reolink_viewer.display import choose_gdk_backend

choose_gdk_backend()  # before Gtk is imported

from reolink_viewer.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
