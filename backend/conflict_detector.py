"""Duplicate/conflict detection across the installed mod library.

Two signals, both derived entirely from data already tracked in mod_files
(no new file parsing needed — every file's hash is already computed at
install time by mod_manager.py, and kept current by scanner.py):

  - .package duplicates: two or more mods ship a byte-identical file (same
    hash). Usually the same mod installed twice under different names, or
    one mod bundling another's file wholesale.
  - .ts4script name collisions: two or more mods ship a script file with the
    same filename at their mod root (.ts4script files are always flattened
    to the mod root — see mod_manager.py). Since .ts4script archives are
    Python zipimport archives and the interpreter's module cache keys on
    module name, not file path, two mods shipping a same-named script —
    often each bundling their own copy of a shared library like
    sims4communitylib — can silently shadow each other: a well-documented
    source of confusing bugs in the modding community.

Purely informational: this never blocks install/enable and never suggests
deleting anything — same "suspicion is not confirmation" spirit as
crash_analyzer.py's suspects (CLAUDE.md's "Things to never do").
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ConflictGroup:
    kind: str  # 'duplicate_package' | 'ts4script_name_collision'
    identifier: str  # the shared hash, or the shared filename
    mod_ids: list[str]


def _mod_ids_sharing(conn: sqlite3.Connection, *, where: str, params: tuple) -> list[str]:
    rows = conn.execute(f"SELECT DISTINCT mod_id FROM mod_files WHERE {where} ORDER BY mod_id", params)
    return [row["mod_id"] for row in rows]


def find_package_duplicates(conn: sqlite3.Connection) -> list[ConflictGroup]:
    hash_rows = conn.execute(
        "SELECT hash FROM mod_files "
        "WHERE extension = '.package' AND hash IS NOT NULL "
        "GROUP BY hash HAVING COUNT(DISTINCT mod_id) > 1"
    ).fetchall()
    return [
        ConflictGroup(
            kind="duplicate_package",
            identifier=row["hash"],
            mod_ids=_mod_ids_sharing(conn, where="hash = ?", params=(row["hash"],)),
        )
        for row in hash_rows
    ]


def find_ts4script_name_collisions(conn: sqlite3.Connection) -> list[ConflictGroup]:
    name_rows = conn.execute(
        "SELECT relative_path FROM mod_files "
        "WHERE extension = '.ts4script' "
        "GROUP BY relative_path HAVING COUNT(DISTINCT mod_id) > 1"
    ).fetchall()
    return [
        ConflictGroup(
            kind="ts4script_name_collision",
            identifier=row["relative_path"],
            mod_ids=_mod_ids_sharing(
                conn, where="relative_path = ? AND extension = '.ts4script'", params=(row["relative_path"],)
            ),
        )
        for row in name_rows
    ]


def find_conflicts(conn: sqlite3.Connection) -> list[ConflictGroup]:
    return find_package_duplicates(conn) + find_ts4script_name_collisions(conn)
