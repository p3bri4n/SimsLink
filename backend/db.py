"""SQLite schema and migrations for SimsLink's local database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Each entry is (version, script). Scripts are applied in order, once, against
# PRAGMA user_version — SQLite's built-in schema version counter. Never edit a
# script that has already shipped; append a new (version, script) entry instead.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE mods (
            id TEXT PRIMARY KEY,                 -- mod_id slug, matches Mods/<id>/ folder name
            curseforge_id INTEGER,
            name TEXT NOT NULL,
            author TEXT,
            category TEXT,
            library_path TEXT NOT NULL,
            primary_type TEXT NOT NULL CHECK (primary_type IN ('package', 'script', 'mixed')),
            installed_version TEXT,
            latest_version TEXT,
            game_version_min TEXT,
            game_version_max TEXT,
            compat_status TEXT NOT NULL DEFAULT 'unknown'
                CHECK (compat_status IN ('compatible', 'incompatible', 'unknown')),
            third_party_distribution_allowed INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            install_date TEXT NOT NULL,
            update_date TEXT,
            thumbnail_url TEXT,
            thumbnail_local TEXT,
            short_description TEXT,
            full_description TEXT,
            screenshots TEXT,                    -- JSON-encoded list of URLs
            links TEXT                           -- JSON-encoded object (curseforge_url, author_site, donation, ...)
        );

        CREATE TABLE mod_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mod_id TEXT NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL,
            hash TEXT,
            extension TEXT NOT NULL,
            UNIQUE (mod_id, relative_path)
        );
        CREATE INDEX idx_mod_files_mod_id ON mod_files(mod_id);

        CREATE TABLE dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mod_id TEXT NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
            depends_on_curseforge_id INTEGER,
            dependency_type TEXT NOT NULL
                CHECK (dependency_type IN ('required', 'optional', 'translation')),
            confidence TEXT NOT NULL DEFAULT 'confirmed'
                CHECK (confidence IN ('confirmed', 'suggested')),
            mandatory INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_dependencies_mod_id ON dependencies(mod_id);

        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE profile_mods (
            profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
            mod_id TEXT NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
            PRIMARY KEY (profile_id, mod_id)
        );

        CREATE TABLE crash_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            raw_last_exception TEXT NOT NULL,
            auto_suspect_mods TEXT,              -- JSON-encoded list
            active_mods_snapshot TEXT,            -- JSON-encoded list
            bisection_in_progress INTEGER NOT NULL DEFAULT 0,
            bisection_history TEXT,               -- JSON-encoded list of tested batches + results
            confirmed_faulty_mod_id TEXT REFERENCES mods(id) ON DELETE SET NULL,
            user_note TEXT
        );
        """,
    ),
    (
        2,
        """
        -- scanner.py's incremental scan needs a prior size+mtime to compare
        -- each file against; migration 1 omitted them, so add them here.
        ALTER TABLE mod_files ADD COLUMN size INTEGER;
        ALTER TABLE mod_files ADD COLUMN mtime REAL;
        """,
    ),
    (
        3,
        """
        -- dependencies.depends_on_curseforge_id alone can't express a link
        -- between two locally-installed mods when neither has a known
        -- curseforge_id yet (the normal case in Assisted Mode, since there's
        -- no API to populate it) — add a local-id alternative.
        ALTER TABLE dependencies ADD COLUMN depends_on_mod_id TEXT REFERENCES mods(id) ON DELETE SET NULL;
        """,
    ),
    (
        4,
        """
        -- Local, user-editable blacklist of known-bad mod name/id patterns
        -- (blacklist.py) — a simplified version of SimsForge's malicious-mod
        -- flagging (see CLAUDE.md). Purely local: nothing fetches a shared
        -- list, matching only ever informs, never blocks install.
        CREATE TABLE blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT NOT NULL,
            note TEXT,
            created_date TEXT NOT NULL
        );
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0]


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply any migration scripts newer than the DB's current user_version."""
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in MIGRATIONS:
        if version > current_version:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database and bring its schema up to date."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)
    return conn
