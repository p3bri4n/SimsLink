"""Shared timestamped-backup helper, under LIBRARY_DIR/.backups/.

Originally lived only in download_watcher.py (for mod replace/update), and
is reused as-is by broken_mods.py's automatic fixes: any destructive action
this project takes on a folder it didn't create should be reversible, per
the same "always require confirmation before deleting" rule used everywhere
else (CLAUDE.md's Cache cleanup section).
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import Config


def backup_folder(source: Path, key: str, config: Config) -> None:
    """Copies `source` into LIBRARY_DIR/.backups/<key>-<UTC timestamp>/, then
    purges old backups for `key` beyond config.backup_retention_count."""
    if not source.exists():
        return
    backups_dir = config.library_dir / ".backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copytree(source, backups_dir / f"{key}-{timestamp}")
    purge_old_backups(backups_dir, key, config.backup_retention_count)


def purge_old_backups(backups_dir: Path, key: str, keep: int) -> None:
    """Keeps only the `keep` most recent backups for `key`; every backup call
    creates one more, and nothing purges the rest on its own."""
    # Backup dirs are named "<key>-<UTC timestamp>"; the timestamp format
    # (%Y%m%dT%H%M%SZ) sorts lexicographically in chronological order, so a
    # plain name sort finds the oldest ones without parsing anything.
    existing = sorted(d for d in backups_dir.glob(f"{key}-*") if d.is_dir())
    to_delete = existing[:-keep] if keep > 0 else existing
    for old in to_delete:
        shutil.rmtree(old)
