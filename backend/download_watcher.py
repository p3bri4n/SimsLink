"""Watches the download folder for new mod files (Assisted Mode).

Detects new .zip/.package/.ts4script files; the caller decides whether one
looks like an update to an already-installed mod (via match_existing_mod)
and installs/replaces only after the user confirms — this module never
installs anything on its own. Per CLAUDE.md: "Never auto-suggest deletion"
applies just as much to silently replacing a mod the user didn't ask to
replace.

The watcher notifies on its own background thread (a watchdog observer
thread), so it deliberately never touches the sqlite3 connection itself —
sqlite3 connections aren't safe to use across threads. Callers should marshal
match_existing_mod()/confirm_*() calls back onto whichever thread owns `conn`
(e.g. a FastAPI request handler, which gets its own connection per request —
see backend/main.py's get_conn()).
"""

from __future__ import annotations

import difflib
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import mod_manager
from .config import Config

_WATCHED_EXTENSIONS = {".zip", ".package", ".ts4script"}
_MATCH_CUTOFF = 0.6


class DownloadWatcherError(Exception):
    pass


def match_existing_mod(path: Path, conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """Filename-proximity match against installed mods. Returns (None, None)
    when there's no match or the match is ambiguous — never guess silently."""
    target_slug = mod_manager.slugify(path.stem)
    rows = conn.execute("SELECT id, name FROM mods").fetchall()
    by_slug: dict[str, sqlite3.Row] = {}
    for row in rows:
        by_slug[mod_manager.slugify(row["name"])] = row
        by_slug.setdefault(mod_manager.slugify(row["id"]), row)

    matches = difflib.get_close_matches(target_slug, by_slug.keys(), n=2, cutoff=_MATCH_CUTOFF)
    if len(matches) != 1:
        return None, None
    row = by_slug[matches[0]]
    return row["id"], row["name"]


def _backup_library_folder(library_path: Path, config: Config) -> None:
    if not library_path.exists():
        return
    backups_dir = config.library_dir / ".backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copytree(library_path, backups_dir / f"{library_path.name}-{timestamp}")
    _purge_old_backups(backups_dir, library_path.name, config.backup_retention_count)


def _purge_old_backups(backups_dir: Path, mod_id: str, keep: int) -> None:
    """Keeps only the `keep` most recent backups for `mod_id`; every replace
    creates one more, and nothing was purging the rest before this — left
    unbounded, .backups/ grows forever (CLAUDE.md's Settings §6.8 gap)."""
    # Backup dirs are named "<mod_id>-<UTC timestamp>"; the timestamp format
    # (%Y%m%dT%H%M%SZ) sorts lexicographically in chronological order, so a
    # plain name sort finds the oldest ones without parsing anything.
    existing = sorted(d for d in backups_dir.glob(f"{mod_id}-*") if d.is_dir())
    to_delete = existing[:-keep] if keep > 0 else existing
    for old in to_delete:
        shutil.rmtree(old)


def confirm_install(
    path: Path, *, config: Config, conn: sqlite3.Connection, mod_name: str | None = None
) -> str:
    """Installs a freshly detected download as a new mod. Call only after the
    user has confirmed via the UI."""
    return mod_manager.install(path, config=config, conn=conn, mod_name=mod_name)


def confirm_replace(
    path: Path,
    mod_id: str,
    *,
    config: Config,
    conn: sqlite3.Connection,
    metadata: mod_manager.ModMetadata | None = None,
) -> str:
    """Replaces `mod_id` with this newly downloaded version, backing up the
    old library folder first. Call only after the user has confirmed via the
    UI. `metadata` lets Direct Mode callers (ui/updates.py) carry the fresh
    CurseForge metadata through the replace — Assisted Mode callers omit it,
    same as a plain install."""
    old_row = conn.execute("SELECT name, library_path FROM mods WHERE id = ?", (mod_id,)).fetchone()
    if old_row is None:
        raise DownloadWatcherError(f"No such mod: {mod_id}")

    _backup_library_folder(Path(old_row["library_path"]), config)
    mod_manager.delete(mod_id, config=config, conn=conn)
    return mod_manager.install(
        path, config=config, conn=conn, mod_name=old_row["name"], metadata=metadata or mod_manager.ModMetadata()
    )


class _DownloadEventHandler(FileSystemEventHandler):
    def __init__(self, on_event: Callable[[Path], None]) -> None:
        self._on_event = on_event

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._on_event(Path(event.src_path))

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._on_event(Path(event.src_path))


class DownloadWatcher:
    """Watches config.download_watch_dir for new mod archives/files.

    Debounces on write-completion: a file is only reported once no new
    create/modify event has arrived for it for `settle_seconds` — download
    tools write in chunks, so an immediate "created" event usually means a
    still-incomplete file. `on_detected(path)` fires on a background thread.
    """

    def __init__(
        self,
        config: Config,
        on_detected: Callable[[Path], None],
        *,
        settle_seconds: float = 1.0,
    ) -> None:
        self._config = config
        self._on_detected = on_detected
        self._settle_seconds = settle_seconds
        self._observer = Observer()
        self._timers: dict[Path, threading.Timer] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        self._config.download_watch_dir.mkdir(parents=True, exist_ok=True)
        handler = _DownloadEventHandler(self._schedule_check)
        self._observer.schedule(handler, str(self._config.download_watch_dir), recursive=False)
        self._observer.start()

    def stop(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
        self._observer.stop()
        self._observer.join(timeout=5)

    def _schedule_check(self, path: Path) -> None:
        if path.suffix.lower() not in _WATCHED_EXTENSIONS:
            return
        with self._lock:
            existing = self._timers.get(path)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(self._settle_seconds, self._report, args=(path,))
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def _report(self, path: Path) -> None:
        with self._lock:
            self._timers.pop(path, None)
        if path.is_file():
            self._on_detected(path)
