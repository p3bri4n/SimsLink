"""SimsLink's desktop entry point. Starts the FastAPI app (backend/main.py)
on a background thread, waits for it to come up, then opens it in the
system's default browser.

Temporarily browser-only instead of a pywebview native window: on this
project's dev machine (GTK3 WebKit2GTK under a native Wayland session),
real mouse clicks silently failed to reach the page (page rendered, keyboard
input worked, but click events never fired) — reproduced under both the
native Wayland backend and GDK_BACKEND=x11 (XWayland), so not a simple
backend-selection fix. Revisit pywebview once that's root-caused; nothing
about the backend (FastAPI routes, frontend JS) depends on which shell
serves it. See CLAUDE.md's "Current project status" for more detail.
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser

import uvicorn
from fastapi import FastAPI

from backend.config import Config, ConfigError
from backend.logging_config import configure_logging
from backend.main import create_app

HOST = "127.0.0.1"
PORT = 8000


def _start_server(app: FastAPI, log_level: str) -> uvicorn.Server:
    # Reuses the same LOG_LEVEL as our own app logger (see logging_config.py)
    # so one setting controls both, rather than uvicorn's access log staying
    # hardcoded independent of it.
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level=log_level.lower()))
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

    configure_logging(config)
    app = create_app(config)
    server = _start_server(app, config.log_level)
    # Real-time detection of new downloads (Assisted Mode) and of external
    # changes under Mods/ — see backend/main.py's create_app() for why
    # neither is started there. The startup catch-up scan runs in its own
    # thread so it never delays opening the window (CLAUDE.md: "must never
    # block the UI"). The Mods/ watcher is the only one of the two that's
    # user-configurable (MODS_WATCHER_ENABLED) — .stop() on a watcher that
    # was never .start()ed raises (watchdog's Observer is a Thread; joining
    # one that never started is an error), so the guard has to match on
    # both ends, not just skip the start() call.
    app.state.download_watcher.start()
    if config.mods_watcher_enabled:
        app.state.mods_watcher.start()
    threading.Thread(target=app.state.run_startup_scan, daemon=True).start()

    url = f"http://{HOST}:{PORT}/"
    webbrowser.open(url)
    print(f"SimsLink is running at {url} — press Ctrl+C to stop.")
    try:
        while not server.should_exit:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.state.download_watcher.stop()
        if config.mods_watcher_enabled:
            app.state.mods_watcher.stop()
        server.should_exit = True


if __name__ == "__main__":
    main()
