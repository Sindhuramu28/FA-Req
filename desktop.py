"""Native desktop launcher for SyncWorkTrack."""

import webview

from app import app


def main() -> None:
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
