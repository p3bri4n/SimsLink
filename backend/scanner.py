"""Incremental + full mod scanning, real-time Mods/ watcher, and import of
mods that were placed directly under Mods/ before SimsLink managed them."""

from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import mod_manager
from .config import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanStats:
    mods_scanned: int = 0
    files_hashed: int = 0
    files_unchanged: int = 0
    files_removed: int = 0


def _scan_mod(
    conn: sqlite3.Connection, mod_id: str, library_path: Path, *, force_hash: bool
) -> tuple[int, int, int]:
    """Returns (files_hashed, files_unchanged, files_removed) for one mod."""
    existing = {
        row["relative_path"]: row
        for row in conn.execute(
            "SELECT relative_path, size, mtime FROM mod_files WHERE mod_id = ?",
            (mod_id,),
        )
    }
    seen: set[str] = set()
    hashed = unchanged = 0

    for mod_file in mod_manager.walk_mod_files(library_path):
        rel = mod_file.relative_path.as_posix()
        seen.add(rel)
        abs_path = library_path / mod_file.relative_path
        stat = abs_path.stat()
        prior = existing.get(rel)
        needs_hash = (
            force_hash
            or prior is None
            or prior["size"] != stat.st_size
            or prior["mtime"] != stat.st_mtime
        )
        if needs_hash:
            file_hash = mod_manager.hash_file(abs_path)
            conn.execute(
                "INSERT INTO mod_files (mod_id, relative_path, hash, extension, size, mtime) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (mod_id, relative_path) DO UPDATE SET "
                "hash = excluded.hash, size = excluded.size, mtime = excluded.mtime",
                (mod_id, rel, file_hash, mod_file.extension, stat.st_size, stat.st_mtime),
            )
            hashed += 1
        else:
            unchanged += 1

    removed = 0
    for rel in existing.keys() - seen:
        conn.execute(
            "DELETE FROM mod_files WHERE mod_id = ? AND relative_path = ?", (mod_id, rel)
        )
        removed += 1

    return hashed, unchanged, removed


def incremental_scan(config: Config, conn: sqlite3.Connection) -> ScanStats:
    """Compares size+mtime against the last known state; only new/changed
    files get re-hashed, unchanged mod folders cost zero hash computation."""
    mods_scanned = files_hashed = files_unchanged = files_removed = 0
    for row in conn.execute("SELECT id, library_path FROM mods").fetchall():
        hashed, unchanged, removed = _scan_mod(
            conn, row["id"], Path(row["library_path"]), force_hash=False
        )
        mods_scanned += 1
        files_hashed += hashed
        files_unchanged += unchanged
        files_removed += removed
    conn.commit()
    return ScanStats(mods_scanned, files_hashed, files_unchanged, files_removed)


def full_scan(
    config: Config, conn: sqlite3.Connection, *, max_workers: int | None = None
) -> ScanStats:
    """Recomputes hashes for every tracked file, hashing in parallel across
    processes since hashing is CPU-bound. Manual-only (Settings > Full scan)."""
    mods = conn.execute("SELECT id, library_path FROM mods").fetchall()

    targets: list[tuple[str, mod_manager.ModFile, Path]] = []
    for row in mods:
        library_path = Path(row["library_path"])
        for mod_file in mod_manager.walk_mod_files(library_path):
            targets.append((row["id"], mod_file, library_path / mod_file.relative_path))

    if not targets:
        return ScanStats(mods_scanned=len(mods))

    paths = [t[2] for t in targets]
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        hashes = list(pool.map(mod_manager.hash_file, paths))

    seen_by_mod: dict[str, set[str]] = {row["id"]: set() for row in mods}
    for (mod_id, mod_file, abs_path), file_hash in zip(targets, hashes):
        rel = mod_file.relative_path.as_posix()
        seen_by_mod[mod_id].add(rel)
        stat = abs_path.stat()
        conn.execute(
            "INSERT INTO mod_files (mod_id, relative_path, hash, extension, size, mtime) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (mod_id, relative_path) DO UPDATE SET "
            "hash = excluded.hash, size = excluded.size, mtime = excluded.mtime",
            (mod_id, rel, file_hash, mod_file.extension, stat.st_size, stat.st_mtime),
        )

    files_removed = 0
    for row in mods:
        existing = {
            r["relative_path"]
            for r in conn.execute(
                "SELECT relative_path FROM mod_files WHERE mod_id = ?", (row["id"],)
            )
        }
        for rel in existing - seen_by_mod[row["id"]]:
            conn.execute(
                "DELETE FROM mod_files WHERE mod_id = ? AND relative_path = ?",
                (row["id"], rel),
            )
            files_removed += 1

    conn.commit()
    return ScanStats(
        mods_scanned=len(mods),
        files_hashed=len(targets),
        files_unchanged=0,
        files_removed=files_removed,
    )


def import_untracked_mods(config: Config, conn: sqlite3.Connection) -> list[str]:
    """Imports mods placed directly under Mods/ before SimsLink managed them.

    Any real (non-symlink) directory under Mods/ that isn't a known mod id is
    treated as unmanaged: its contents move into the library (applying the
    same .ts4script flattening rule as mod_manager.install) and it's replaced
    by a managed symlink.
    """
    if not config.sims4_mods_dir.exists():
        return []

    known_ids = {row["id"] for row in conn.execute("SELECT id FROM mods")}
    imported: list[str] = []

    for entry in sorted(config.sims4_mods_dir.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name in known_ids:
            continue
        try:
            mod_id = mod_manager.import_existing_folder(entry, config=config, conn=conn)
        except mod_manager.ModManagerError:
            # A folder under Mods/ that isn't importable (e.g. no
            # .package/.ts4script anywhere in it — an extracted .ts4script
            # left as loose .pyc files, a readme-only folder, ...) shouldn't
            # abort the whole scan and leave every later entry unimported.
            logger.warning("Skipping unimportable folder under Mods/: %s", entry, exc_info=True)
            continue
        imported.append(mod_id)

    return imported


class _ChangeHandler(FileSystemEventHandler):
    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change

    def on_any_event(self, event) -> None:
        self._on_change()


class ModsFolderWatcher:
    """Watches Mods/ for external changes while the app is running.

    Only notifies `on_change` — it doesn't decide when to re-scan; callers
    debounce and trigger incremental_scan() themselves.
    """

    def __init__(self, config: Config, on_change: Callable[[], None]) -> None:
        self._mods_dir = config.sims4_mods_dir
        self._on_change = on_change
        self._observer = Observer()

    def start(self) -> None:
        handler = _ChangeHandler(self._on_change)
        self._observer.schedule(handler, str(self._mods_dir), recursive=True)
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join(timeout=5)
