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
    (
        5,
        """
        -- Profiles gained a timestamp so a saved "library state" shows when
        -- it was captured, not just its name — nullable since profiles
        -- created before this migration have no timestamp to backfill.
        ALTER TABLE profiles ADD COLUMN created_date TEXT;
        """,
    ),
    (
        6,
        """
        -- Reminders for a mod that was deleted while it was still part of a
        -- saved state (profiles.py's record_missing_mod_if_saved()) — name
        -- and curseforge_url are captured at deletion time, before the
        -- mods row (and, via profile_mods' ON DELETE CASCADE, every
        -- profile's membership record of it) disappears for good. No FK to
        -- mods(id): the whole point is to outlive that row. UNIQUE on
        -- mod_id so re-detecting the same missing mod (e.g. loading a
        -- second stale save, or deleting it again after a reinstall) never
        -- creates a duplicate reminder.
        CREATE TABLE missing_mods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mod_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            curseforge_url TEXT,
            source_profile_names TEXT NOT NULL,
            detected_date TEXT NOT NULL
        );
        """,
    ),
    (
        7,
        """
        -- Generic key/value overrides, so far used only by path_settings.py
        -- for the three editable installation paths (SIMS4_GAME_DIR,
        -- SIMS4_USER_DIR, LIBRARY_DIR) — layered on top of the .env-derived
        -- Config at startup (config.py's from_env() stays the bootstrap;
        -- this is what lets Settings change them afterward without editing
        -- .env by hand).
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """,
    ),
    (
        8,
        """
        -- Marks a mod adopted from a loose .package/.ts4script file sitting
        -- directly at Mods/ root (loose_mods.py's import_loose_files()) —
        -- surfaced as a "Loose"/"Vrac" tag so it's easy to tell apart from a
        -- mod installed through a normal archive, and to find later for
        -- loose_mods.suggest_groupings()'s "these look related" merge tool.
        ALTER TABLE mods ADD COLUMN is_loose_import INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        9,
        """
        -- A manual correction for the Library's inferred grouping label
        -- (frontend/app.js's groupingAuthor()) — that label is frequently a
        -- *guessed* mod/series namespace (a bracket prefix, or a
        -- shared-name-segments cluster), not a confirmed author, and the
        -- guess can be wrong or nonsensical. Deliberately a separate column
        -- from `author` (real data, only ever populated from CurseForge)
        -- rather than overwriting it — this is "how I want this mod grouped
        -- in my Library," not a claim about who made it. NULL means no
        -- manual correction; grouping falls back to the normal
        -- author/prefix/cluster inference.
        ALTER TABLE mods ADD COLUMN namespace_override TEXT;
        """,
    ),
    (
        10,
        """
        -- The mod's real, human-authored name as CurseForge itself has it —
        -- distinct from `name` (the locally-derived name a mod was installed
        -- under, e.g. a raw filename for a loose import: see loose_mods.py).
        -- NULL until curseforge_match.py's _apply_match() (or a future
        -- catalog-install path) fills it in. The frontend prefers this over
        -- `name` whenever it's set, and — unlike `name` — it's never subject
        -- to the "Simplified names" toggle's stripping/cleanup, since it's
        -- already the real, correct name rather than something guessed at
        -- from messy local text.
        ALTER TABLE mods ADD COLUMN curseforge_name TEXT;
        """,
    ),
    (
        11,
        """
        -- Tracks exactly which mods compat_quarantine.py's "disable
        -- incompatible mods (+ their local required dependents)" action
        -- turned off, so release_ready_mods() can bring back precisely
        -- those once each one's own compat_status clears up after a
        -- CurseForge resync — deliberately not a full active-set snapshot
        -- like profiles.py's save/load, just the mods this one action is
        -- responsible for. `reason` is 'incompatible' for a seed mod, or the
        -- mod_id of the dependency that pulled a dependent mod in via the
        -- cascade. ON DELETE CASCADE: nothing to reactivate once the mod
        -- itself is gone.
        CREATE TABLE compat_quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mod_id TEXT NOT NULL UNIQUE REFERENCES mods(id) ON DELETE CASCADE,
            reason TEXT NOT NULL,
            quarantined_date TEXT NOT NULL
        );
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0]


def connect(db_path: Path) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI/Starlette dispatch sync dependencies
    # via anyio's worker thread pool, so a single request's __enter__ (this
    # call), route handler, and __exit__ (conn.close()) can each land on a
    # different pool thread even though they're always sequential, never
    # concurrent, for a given connection. sqlite3's default same-thread
    # check doesn't know that and raises regardless.
    conn = sqlite3.connect(db_path, check_same_thread=False)
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
