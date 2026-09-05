"""The three editable installation paths (SIMS4_GAME_DIR, SIMS4_USER_DIR,
LIBRARY_DIR) — settable from the Settings view, not just `.env`.

`.env` (via config.py's `Config.from_env()`) stays the *bootstrap*: it's
still what a fresh install needs to start at all. What this module adds is
a `settings` key/value table that layers overrides on top of that at
startup (`apply_stored_overrides()`, called once by `main.py`'s
`create_app()`) and lets the user change them afterward
(`update_paths()`, called by `POST /api/settings/paths`).

Changing SIMS4_USER_DIR or LIBRARY_DIR after mods are already installed
isn't a plain value swap: `mods.library_path` is stored as an absolute path
per mod, and every active mod has a real symlink under the *old*
`Mods/`. Silently repointing the setting without moving anything would
leave every mod's `library_path` stale and every symlink dangling — the
exact kind of silent breakage `migrate_legacy_data_dir()` (config.py) had
to clean up after once already. So `update_paths()` physically migrates:
moves the library folder's real contents (if `library_dir` changed) via
`shutil.move()` (a same-filesystem rename when possible; only removes the
source after a cross-filesystem copy has fully succeeded, so a failure
midway leaves the original untouched rather than half-deleted), rewrites
every `mods.library_path` row to match, and re-creates every active mod's
symlink through the new `Mods/` location — no separate whole-library
backup on top of that, since the move itself doesn't destroy the source
until the destination is confirmed complete.

The currently-running process keeps whatever Config it already loaded for
anything that captured a *snapshot* of it at construction time — most
notably `scanner.ModsFolderWatcher`, which stores the directory it's
watching once, at `.start()`. `update_paths()` itself takes effect
immediately for the shared Config every route reads live (main.py reassigns
its closed-over `config` right after calling this), but the watcher won't
retarget until the app is restarted — the same "changed here, applies after
restart" contract already used for LOG_LEVEL/MODS_WATCHER_ENABLED.
"""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
from pathlib import Path

from . import mod_manager
from .config import Config

_GAME_EXE_CANDIDATES = ("Game/Bin/TS4_x64.exe", "Game/Bin/TS4_DX9_x64.exe")
_SETTINGS_KEYS = ("sims4_game_dir", "sims4_user_dir", "library_dir")


class PathValidationError(Exception):
    """A hard coherence rule was violated — the change is refused outright,
    unlike the softer game-folder check in validate_paths(), which only
    warns."""


def validate_paths(sims4_game_dir: Path, sims4_user_dir: Path, library_dir: Path) -> list[str]:
    """Raises PathValidationError on a hard rule violation. Otherwise
    returns a list of non-blocking warning *codes* (not sentences — the
    frontend maps each through i18n, same as every other user-facing string
    in this app; only PathValidationError's message is raw English text,
    following this codebase's existing convention of passing exception
    messages straight through to the UI's generic "Error: {error}" template)
    that the caller can still choose to save past — Proton/Steam library
    layouts vary enough that a hard block here would refuse legitimate
    setups.
    """
    game = sims4_game_dir.resolve()
    user = sims4_user_dir.resolve()
    library = library_dir.resolve()

    if len({game, user, library}) != 3:
        raise PathValidationError(
            "The game folder, game user folder, and library folder must all be different."
        )

    mods_dir = user / "Mods"
    if library == mods_dir or mods_dir in library.parents or library in mods_dir.parents:
        raise PathValidationError(
            "The library folder can't be inside, or contain, the game's Mods/ folder — "
            "that would create recursive symlinks."
        )

    warnings = []
    if not any((game / candidate).is_file() for candidate in _GAME_EXE_CANDIDATES):
        warnings.append("game_dir_no_executable")
    return warnings


def load_path_overrides(conn: sqlite3.Connection) -> dict[str, Path]:
    placeholders = ", ".join("?" * len(_SETTINGS_KEYS))
    rows = conn.execute(
        f"SELECT key, value FROM settings WHERE key IN ({placeholders})", _SETTINGS_KEYS
    ).fetchall()
    return {row["key"]: Path(row["value"]) for row in rows}


def apply_stored_overrides(config: Config, conn: sqlite3.Connection) -> Config:
    """Called once at startup (main.py's create_app(), before anything else
    captures `config` in a closure) to layer any settings-table path
    overrides on top of the .env-derived Config. A fresh DB with no
    settings rows yet — every test, or a real first launch — returns
    `config` unchanged."""
    overrides = load_path_overrides(conn)
    return dataclasses.replace(config, **overrides) if overrides else config


def update_paths(
    config: Config,
    conn: sqlite3.Connection,
    *,
    sims4_game_dir: Path | None = None,
    sims4_user_dir: Path | None = None,
    library_dir: Path | None = None,
) -> tuple[Config, list[str]]:
    """Validates and applies a change to one or more of the three editable
    paths — arguments left as None keep their current value. Returns
    (new_config, warnings); see validate_paths() for what "warnings" means
    and the module docstring for what actually gets migrated and why.
    """
    new_game_dir = sims4_game_dir or config.sims4_game_dir
    new_user_dir = sims4_user_dir or config.sims4_user_dir
    new_library_dir = library_dir or config.library_dir

    warnings = validate_paths(new_game_dir, new_user_dir, new_library_dir)

    new_config = dataclasses.replace(
        config,
        sims4_game_dir=new_game_dir,
        sims4_user_dir=new_user_dir,
        library_dir=new_library_dir,
    )

    library_changed = new_library_dir.resolve() != config.library_dir.resolve()
    user_dir_changed = new_user_dir.resolve() != config.sims4_user_dir.resolve()

    if library_changed:
        _migrate_library(config, new_config, conn)
    if library_changed or user_dir_changed:
        _relink_active_mods(config, new_config, conn)

    for key, value in (
        ("sims4_game_dir", new_game_dir),
        ("sims4_user_dir", new_user_dir),
        ("library_dir", new_library_dir),
    ):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
    conn.commit()

    return new_config, warnings


def _migrate_library(old_config: Config, new_config: Config, conn: sqlite3.Connection) -> None:
    """Moves the library folder's real contents to the new location — see
    the module docstring for why a plain setting swap isn't safe here.
    shutil.move() only removes the source once the destination is fully
    written (a rename on the same filesystem, a copy-then-cleanup across
    filesystems), so a failure partway through leaves the original intact.
    """
    old_dir = old_config.library_dir
    new_dir = new_config.library_dir
    if old_dir.is_dir():
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_dir), str(new_dir))
    else:
        new_dir.mkdir(parents=True, exist_ok=True)

    for row in conn.execute("SELECT id FROM mods").fetchall():
        conn.execute(
            "UPDATE mods SET library_path = ? WHERE id = ?",
            (str(new_dir / row["id"]), row["id"]),
        )
    conn.commit()


def _relink_active_mods(old_config: Config, new_config: Config, conn: sqlite3.Connection) -> None:
    """Re-creates every active mod's symlink under the new Mods/ location.
    Needed whenever library_dir moves (the old symlink's target is gone)
    or sims4_user_dir moves (Mods/ itself is now a different folder) —
    either way, the old link is removed first (best-effort: the old Mods/
    folder may not even exist anymore, e.g. if the whole user dir moved)."""
    active_ids = [row["id"] for row in conn.execute("SELECT id FROM mods WHERE active = 1")]
    for mod_id in active_ids:
        old_link = old_config.sims4_mods_dir / mod_id
        if old_link.is_symlink():
            old_link.unlink()
        elif old_link.is_dir():
            shutil.rmtree(old_link)
        mod_manager.activate_symlink(mod_id, new_config)
