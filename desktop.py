"""Desktop entry point for the FastAPI + pywebview build (see CLAUDE.md's
"Tech stack"/"Architecture"). Starts the FastAPI app (backend/main.py) on a
background thread, waits for it to come up, then opens it in a native
window via pywebview — not a browser tab.

This is the target replacement for main.py's Flet app once the migration
described in CLAUDE.md's "Current project status" is complete. For now it
only serves the Library-view vertical slice; main.py (Flet) remains the
full, working app in the meantime.

Linux system deps for pywebview's GTK/WebKit backend: `python3-gi` +
`gir1.2-webkit2-4.0` (see CLAUDE.md's "Tech stack").
"""

from __future__ import annotations

import sys
import threading
import time

import uvicorn
import webview

from backend.main import create_app
from config import Config, ConfigError

HOST = "127.0.0.1"
PORT = 8000


def _start_server(config: Config) -> uvicorn.Server:
    app = create_app(config)
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

    server = _start_server(config)
    webview.create_window("SimsLink", f"http://{HOST}:{PORT}/", width=1280, height=800, min_size=(960, 600))
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    main()
