"""Backend logging setup — see CLAUDE.md's "Logging" section.

Only called from desktop.py (the production entry point), never from
create_app() itself — same "app creation stays side-effect-free" rule as
download_watcher/mods_watcher/run_startup_scan (see backend/main.py):
building an app inside a test must never reconfigure global logging state
or write a log file, since tests build many throwaway apps per run.

Configures the "backend" logger specifically (not the root logger) — every
module in this package gets one via `logging.getLogger(__name__)`, which
resolves to a name like "backend.main" and propagates up to "backend" by
Python's normal logger hierarchy, so setting the level/handlers once here
covers all of them without touching third-party loggers (uvicorn, watchdog,
...) that aren't ours to configure.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Config

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(config: Config, *, log_path: Path | None = None) -> None:
    """`log_path` defaults to `config.log_path` (the real, fixed XDG data
    location — see config.py) for production use, same override pattern
    `backend/main.py`'s `create_app(db_path=...)` uses for the database:
    tests pass an isolated tmp_path file instead of writing into that real
    shared location."""
    log_path = log_path or config.log_path

    logger = logging.getLogger("backend")
    logger.setLevel(config.log_level)
    logger.handlers.clear()  # idempotent: safe to call more than once

    formatter = logging.Formatter(_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
