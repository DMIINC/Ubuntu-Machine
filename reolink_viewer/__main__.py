from reolink_viewer.display import prepare_display

prepare_display()  # before Gtk and GStreamer are initialised

from reolink_viewer.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
