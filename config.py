"""Application configuration loaded from environment variables (.env)."""

from __future__ import annotations

import functools
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

REQUIRED_VARS = (
    "SIMS4_GAME_DIR",
    "SIMS4_MODS_DIR",
    "SIMS4_USER_DIR",
    "LIBRARY_DIR",
)

DEFAULT_DOWNLOAD_WATCH_DIR = Path.home() / "Downloads"

# The SQLite DB isn't user-configurable — it's app state, not a mod library
# concern — so it lives in the standard XDG data location, not LIBRARY_DIR.
DEFAULT_DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "simslink"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    sims4_game_dir: Path
    sims4_mods_dir: Path
    sims4_user_dir: Path
    library_dir: Path
    curseforge_api_key: str | None
    download_watch_dir: Path
    game_version: str | None

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

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> Config:
        # Real environment variables take precedence over the .env file, matching
        # python-dotenv's own default (override=False) and standard 12-factor practice.
        values = {**dotenv_values(env_path or Path(".env")), **os.environ}

        missing = [name for name in REQUIRED_VARS if not values.get(name, "").strip()]
        if missing:
            raise ConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}"
            )

        download_watch_dir = values.get("DOWNLOAD_WATCH_DIR", "").strip()
        game_version = values.get("GAME_VERSION", "").strip() or None
        api_key = values.get("CURSEFORGE_API_KEY", "").strip() or None

        return cls(
            sims4_game_dir=Path(values["SIMS4_GAME_DIR"]).expanduser(),
            sims4_mods_dir=Path(values["SIMS4_MODS_DIR"]).expanduser(),
            sims4_user_dir=Path(values["SIMS4_USER_DIR"]).expanduser(),
            library_dir=Path(values["LIBRARY_DIR"]).expanduser(),
            curseforge_api_key=api_key,
            download_watch_dir=(
                Path(download_watch_dir).expanduser()
                if download_watch_dir
                else DEFAULT_DOWNLOAD_WATCH_DIR
            ),
            game_version=game_version,
        )


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
