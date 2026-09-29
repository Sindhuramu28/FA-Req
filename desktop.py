"""Native desktop launcher for FA Sync Console."""

import webview

from app import app


def main() -> None:
    webview.create_window(
        "FA Sync Console",
        app,
        width=1440,
        height=920,
        min_size=(980, 680),
        background_color="#F3F6F5",
        text_select=True,
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
