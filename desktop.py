"""SimsLink's desktop entry point. Starts the FastAPI app (backend/main.py)
on a background thread, waits for it to come up, then opens it in a native
window via pywebview — not a browser tab.

Linux system deps for pywebview's GTK/WebKit backend: `python3-gi` +
`gir1.2-webkit2-4.0` (see CLAUDE.md's "Tech stack").
"""

from __future__ import annotations

import sys
import threading
import time

import uvicorn
import webview
from fastapi import FastAPI

from backend.config import Config, ConfigError
from backend.main import create_app

HOST = "127.0.0.1"
PORT = 8000


def _start_server(app: FastAPI) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    return server


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"SimsLink configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    app = create_app(config)
    server = _start_server(app)
    # Real-time detection of new downloads (Assisted Mode) — see
    # backend/main.py's create_app() for why this isn't started there.
    app.state.download_watcher.start()
    try:
        webview.create_window("SimsLink", f"http://{HOST}:{PORT}/", width=1280, height=800, min_size=(960, 600))
        webview.start()
    finally:
        app.state.download_watcher.stop()
        server.should_exit = True


if __name__ == "__main__":
    main()
