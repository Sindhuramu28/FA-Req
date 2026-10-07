"""Native desktop launcher for SyncWorkTrack."""

import sys

import webview

from app import app, run_saved_schedule


def main() -> None:
    if "--scheduled-run" in sys.argv:
        raise SystemExit(0 if run_saved_schedule() else 1)
    webview.create_window(
        "SyncWorkTrack",
        app,
        width=1440,
        height=920,
        min_size=(980, 680),
        background_color="#F5F7FB",
        text_select=True,
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
