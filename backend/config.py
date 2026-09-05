"""Application configuration loaded from environment variables (.env)."""

from __future__ import annotations

import functools
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

REQUIRED_VARS = (
    "SIMS4_GAME_DIR",
    "SIMS4_USER_DIR",
)

DEFAULT_DOWNLOAD_WATCH_DIR = Path.home() / "Downloads"
DEFAULT_BACKUP_RETENTION_COUNT = 5
DEFAULT_MODS_WATCHER_ENABLED = True
DEFAULT_LOG_LEVEL = "INFO"
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

# Resolved next to the project root (one level up from this file, which
# lives in backend/), not the process's cwd — cwd varies depending on how
# the app is launched and doesn't reliably match the directory .env was
# created in.
DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Everything SimsLink itself owns (DB, log, and — unless LIBRARY_DIR
# overrides it — the mod library and its backups) lives under one dotfolder
# in the user's home directory by default, the conventional "normal" spot
# for a Linux app's own data rather than spreading it across the XDG base
# directories or requiring LIBRARY_DIR to always be set explicitly.
DEFAULT_SIMSLINK_DIR = Path.home() / ".SimsLink"
DEFAULT_DATA_DIR = DEFAULT_SIMSLINK_DIR
DEFAULT_LIBRARY_DIR = DEFAULT_SIMSLINK_DIR / "library"

# Where the DB/log lived before DEFAULT_DATA_DIR moved to ~/.SimsLink/
# (2026-08-22) — see migrate_legacy_data_dir() below.
LEGACY_DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "simslink"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    sims4_game_dir: Path
    sims4_user_dir: Path
    library_dir: Path
    curseforge_api_key: str | None
    download_watch_dir: Path
    game_version: str | None
    backup_retention_count: int = DEFAULT_BACKUP_RETENTION_COUNT
    mods_watcher_enabled: bool = DEFAULT_MODS_WATCHER_ENABLED
    log_level: str = DEFAULT_LOG_LEVEL

    @property
    def sims4_mods_dir(self) -> Path:
        # Never a stored field, let alone a separate env var: Mods/ is
        # always exactly one fixed subfolder of the game's user directory,
        # so keeping it as independent state could only ever agree with
        # this derivation or silently disagree with it — never add real
        # information. Deriving it removes that whole class of bug.
        return self.sims4_user_dir / "Mods"

    @property
    def has_api_key(self) -> bool:
        """Presence check only. Real key *validity* is verified by curseforge.py."""
        return bool(self.curseforge_api_key and self.curseforge_api_key.strip())

    @functools.cached_property
    def symlink_support(self) -> bool:
        return detect_symlink_support(self.library_dir)

    @property
    def db_path(self) -> Path:
        return DEFAULT_DATA_DIR / "simslink.sqlite3"

    @property
    def log_path(self) -> Path:
        return DEFAULT_DATA_DIR / "simslink.log"

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> Config:
        # Real environment variables take precedence over the .env file, matching
        # python-dotenv's own default (override=False) and standard 12-factor practice.
        values = {**dotenv_values(env_path or DEFAULT_ENV_PATH), **os.environ}

        missing = [name for name in REQUIRED_VARS if not values.get(name, "").strip()]
        if missing:
            raise ConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}"
            )

        download_watch_dir = values.get("DOWNLOAD_WATCH_DIR", "").strip()
        library_dir = values.get("LIBRARY_DIR", "").strip()
        game_version = values.get("GAME_VERSION", "").strip() or None
        sims4_game_dir = Path(values["SIMS4_GAME_DIR"]).expanduser()
        sims4_user_dir = Path(values["SIMS4_USER_DIR"]).expanduser()
        if game_version is None:
            # Deferred import: game_options.py imports Config from this same
            # module, so importing it at module load time would cycle.
            from . import game_options

            game_version = game_options.detect_game_version(sims4_game_dir)
        api_key = values.get("CURSEFORGE_API_KEY", "").strip() or None
        backup_retention_count = _parse_backup_retention_count(values.get("BACKUP_RETENTION_COUNT", ""))
        mods_watcher_enabled = _parse_bool(
            values.get("MODS_WATCHER_ENABLED", ""),
            default=DEFAULT_MODS_WATCHER_ENABLED,
            var_name="MODS_WATCHER_ENABLED",
        )
        log_level = _parse_log_level(values.get("LOG_LEVEL", ""))

        return cls(
            sims4_game_dir=sims4_game_dir,
            sims4_user_dir=sims4_user_dir,
            library_dir=Path(library_dir).expanduser() if library_dir else DEFAULT_LIBRARY_DIR,
            curseforge_api_key=api_key,
            download_watch_dir=(
                Path(download_watch_dir).expanduser()
                if download_watch_dir
                else DEFAULT_DOWNLOAD_WATCH_DIR
            ),
            game_version=game_version,
            backup_retention_count=backup_retention_count,
            mods_watcher_enabled=mods_watcher_enabled,
            log_level=log_level,
        )


def _parse_backup_retention_count(raw: str) -> int:
    raw = raw.strip()
    if not raw:
        return DEFAULT_BACKUP_RETENTION_COUNT
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"BACKUP_RETENTION_COUNT must be a whole number, got: {raw!r}") from None
    if value < 1:
        raise ConfigError("BACKUP_RETENTION_COUNT must be at least 1")
    return value


def _parse_bool(raw: str, *, default: bool, var_name: str) -> bool:
    normalized = raw.strip().lower()
    if not normalized:
        return default
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    raise ConfigError(
        f"{var_name} must be a boolean (1/0, true/false, yes/no, on/off), got: {raw!r}"
    )


def _parse_log_level(raw: str) -> str:
    normalized = raw.strip().upper()
    if not normalized:
        return DEFAULT_LOG_LEVEL
    if normalized not in VALID_LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {VALID_LOG_LEVELS}, got: {raw!r}")
    return normalized


def detect_symlink_support(directory: Path) -> bool:
    """Probe whether `directory`'s filesystem supports symlinks.

    Creates and immediately removes a throwaway file + symlink. Falls back to
    copy-based installs (see mod_manager.py) when this returns False.
    """
    directory.mkdir(parents=True, exist_ok=True)
    probe_name = f".simslink-symlink-probe-{uuid.uuid4().hex}"
    target = directory / probe_name
    link = directory / f"{probe_name}-link"
    try:
        target.touch()
        link.symlink_to(target)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def _sqlite_has_any_mods(db_path: Path) -> bool:
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("SELECT 1 FROM mods LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        # Not a real SimsLink DB, or a table-less/corrupt one — nothing
        # worth migrating either way.
        return False


def migrate_legacy_data_dir() -> None:
    """One-time recovery for installs that predate DEFAULT_DATA_DIR moving
    from the XDG data dir (LEGACY_DATA_DIR) to ~/.SimsLink/ (2026-08-22).

    Without this, an existing user's real database/log is silently orphaned
    at the old location: create_app() starts against a brand-new, empty DB
    at the new path instead, with no error — every installed mod just
    disappears from the Library (broken-folder detection still works, since
    that reads Mods/ directly, which is exactly the confusing symptom that
    surfaced this bug). Moves the old db/log over only when the new
    location doesn't already have real mod data of its own, so a second run
    (or a genuinely fresh install that happens to coexist with old XDG
    leftovers) never clobbers anything.

    Deliberately not called from Config.from_env()/create_app() — both are
    also exercised directly by tests, which must never touch a real
    ~/.SimsLink or the real XDG data dir. Only desktop.py calls this, once,
    before create_app() at real startup.
    """
    old_db = LEGACY_DATA_DIR / "simslink.sqlite3"
    if not _sqlite_has_any_mods(old_db):
        return
    new_db = DEFAULT_DATA_DIR / "simslink.sqlite3"
    if _sqlite_has_any_mods(new_db):
        return

    DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    for filename in ("simslink.sqlite3", "simslink.log"):
        old_file = LEGACY_DATA_DIR / filename
        if old_file.is_file():
            shutil.move(str(old_file), str(DEFAULT_DATA_DIR / filename))
