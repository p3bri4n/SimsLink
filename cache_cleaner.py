"""Cache cleanup — targets specific known-safe cache locations under
SIMS4_USER_DIR (see CLAUDE.md's "Cache cleanup" table for the rationale
behind each target).

Never touches: saves, tray files, screenshots, options.ini, resource.cfg —
those simply aren't in the target list below, so they can't be reached by
accident. lastException.txt/lastCrash.txt are likewise excluded on purpose:
they're managed separately by crash_analyzer.py so diagnostic history isn't
lost before analysis.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from config import Config

# "Clear contents, keep the folder" targets preserve any FileCache.cfg/.ini.
_PRESERVE_IN_CACHE_DIR = {"filecache.cfg", "filecache.ini"}

_DELETE_FILE_TARGETS = (
    ("localthumbcache.package", "Regenerates automatically; stale entries after mod changes can cause invalid lookups and crashes."),
    ("localsimtexturecache.package", "Sim texture cache, capped at 100MB — safe to clear if character textures look wrong."),
)
_CLEAR_CONTENTS_TARGETS = (
    ("cache", "Temporary data regenerated automatically by the game."),
    ("cachestr", "Temporary data regenerated automatically by the game."),
)
_DELETE_DIR_TARGETS = (
    ("cachewebkit", "Only exists while the game is running; safe to remove if it persisted after a crash."),
    ("onlinethumbnailcache", "Online gallery thumbnail cache."),
)


@dataclass(frozen=True)
class CacheTarget:
    name: str
    description: str
    exists: bool


def list_cache_targets(config: Config) -> list[CacheTarget]:
    """What clean_cache() would act on, for a confirmation dialog to show."""
    root = config.sims4_user_dir
    all_specs = _DELETE_FILE_TARGETS + _CLEAR_CONTENTS_TARGETS + _DELETE_DIR_TARGETS
    return [CacheTarget(name=name, description=description, exists=(root / name).exists()) for name, description in all_specs]


def clean_cache(config: Config) -> list[str]:
    """Deletes/clears the known-safe cache targets. Returns the names of the
    targets actually acted on. The caller must already have user
    confirmation — this performs no prompting itself."""
    root = config.sims4_user_dir
    cleaned: list[str] = []

    for filename, _ in _DELETE_FILE_TARGETS:
        path = root / filename
        if path.is_file():
            path.unlink()
            cleaned.append(filename)

    for dirname, _ in _CLEAR_CONTENTS_TARGETS:
        path = root / dirname
        if path.is_dir():
            _clear_directory_contents(path, preserve=_PRESERVE_IN_CACHE_DIR)
            cleaned.append(dirname)

    for dirname, _ in _DELETE_DIR_TARGETS:
        path = root / dirname
        if path.is_dir():
            shutil.rmtree(path)
            cleaned.append(dirname)

    return cleaned


def _clear_directory_contents(path: Path, *, preserve: set[str]) -> None:
    for entry in path.iterdir():
        if entry.name.lower() in preserve:
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
