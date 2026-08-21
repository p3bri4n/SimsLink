"""Local, user-editable blacklist of known-bad mod name/id patterns — a
simplified version of SimsForge's malicious-mod flagging (see CLAUDE.md's
"Project" section). Purely local and manual: nothing here fetches a shared
or remote list, and matching only ever informs — same "suspicion is not
confirmation" rule as crash suspects and conflict detection. Never blocks
install/enable on its own.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


class BlacklistError(Exception):
    pass


@dataclass(frozen=True)
class BlacklistEntry:
    id: int
    pattern: str
    note: str | None


def add_entry(pattern: str, conn: sqlite3.Connection, *, note: str | None = None) -> int:
    pattern = pattern.strip()
    if not pattern:
        raise BlacklistError("Blacklist pattern cannot be empty")
    cursor = conn.execute(
        "INSERT INTO blacklist (pattern, note, created_date) VALUES (?, ?, ?)",
        (pattern, note, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cursor.lastrowid


def remove_entry(entry_id: int, conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM blacklist WHERE id = ?", (entry_id,))
    conn.commit()


def list_entries(conn: sqlite3.Connection) -> list[BlacklistEntry]:
    rows = conn.execute("SELECT id, pattern, note FROM blacklist ORDER BY pattern COLLATE NOCASE").fetchall()
    return [BlacklistEntry(id=row["id"], pattern=row["pattern"], note=row["note"]) for row in rows]


def find_matches(mod_name: str, mod_id: str, entries: list[BlacklistEntry]) -> list[BlacklistEntry]:
    """Case-insensitive substring match against both the mod's display name
    and its id/slug. Takes a pre-fetched `entries` list (rather than a conn)
    so a caller checking many mods can call list_entries() once instead of
    once per mod."""
    haystack = f"{mod_name} {mod_id}".lower()
    return [entry for entry in entries if entry.pattern.lower() in haystack]
