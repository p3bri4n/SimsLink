"""Detects folders directly under Mods/ that don't contain anything the game
would actually load, and classifies *why* on a best-effort basis.

This looks at the same population as scanner.import_untracked_mods() (real,
non-symlink directories directly under Mods/ that aren't a known managed mod
id). scan_broken_mods() is a read-only diagnostic, surfaced alongside
conflict_detector.py's findings in the Library warnings banner.

fix_broken_mod() applies a fix for the two reasons safe enough to automate —
'empty' (delete) and 'unextracted_archive' with exactly one archive (extract
via the normal install pipeline) — but only when the frontend calls it after
the user explicitly confirms (same confirm-modal pattern as cache_cleaner.py
and mod deletion), never on its own. attempt_script_repair() covers
'unpacked_script' separately, and is explicitly *not* one of the safe fixes
above — it's a best-effort re-zip that can produce a mod that "installs"
but still doesn't load in-game (see its own docstring). 'unrecognized' has
no automatic action of any kind: it isn't even confidently a mod in the
first place. Same "suspicion is not confirmation" spirit as everywhere else
in this project.

Before either fix touches the filesystem, the folder is backed up via
backups.py (the same LIBRARY_DIR/.backups/ mechanism download_watcher.py
uses before a replace) — an automatic fix acting on data it didn't create
should be reversible, same as every other destructive action in this app.

Reasons a folder can end up here, from real-world cases:

  - 'empty': no files at all, recursively. Usually a mod author's unused
    "optional addons" placeholder folder. Harmless.
  - 'unextracted_archive': contains a .zip but no .package/.ts4script — the
    mod's actual content is presumably inside the zip, which was dropped
    into Mods/ but never extracted.
  - 'unpacked_script': contains loose .py/.pyc files but no .ts4script — a
    .ts4script archive was likely extracted in place instead of being kept
    intact (the game only loads the archive itself, never a directory tree
    of its contents), or a script mod was distributed as raw source instead
    of a packaged .ts4script.
  - 'unrecognized': files are present but none of the above signals match
    (e.g. only a leftover log/readme, or unrelated data). Deliberately not
    guessed further than "something's off here" — could just as easily be
    non-mod data (a save-adjacent file, a tool's log) as a broken mod.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import mod_manager
from .backups import backup_folder
from .config import Config

_SCRIPT_SOURCE_EXTENSIONS = {".py", ".pyc"}
_SAMPLE_FILE_LIMIT = 5

FIXABLE_REASONS = {"empty", "unextracted_archive"}


class BrokenModFixError(Exception):
    """Raised when fix_broken_mod() can't safely apply an automatic fix."""


@dataclass(frozen=True)
class BrokenModFolder:
    name: str
    reason: str  # 'empty' | 'unextracted_archive' | 'unpacked_script' | 'unrecognized'
    file_count: int
    zip_names: list[str] = field(default_factory=list)
    # Relative paths (up to _SAMPLE_FILE_LIMIT, sorted) of what's actually in
    # the folder — lets the UI say concretely what was found ("contains
    # SCCO_Log.log") instead of just a count, especially useful for
    # 'unrecognized' where the reason alone doesn't say much.
    sample_files: list[str] = field(default_factory=list)


def _classify(entry: Path) -> tuple[str, int, list[str], list[str]]:
    files = [f for f in entry.rglob("*") if f.is_file()]
    sample_files = sorted(f.relative_to(entry).as_posix() for f in files)[:_SAMPLE_FILE_LIMIT]
    if not files:
        return "empty", 0, [], []

    zip_names = [f.name for f in files if f.suffix.lower() == ".zip"]
    if zip_names:
        return "unextracted_archive", len(files), sorted(zip_names), sample_files

    if any(f.suffix.lower() in _SCRIPT_SOURCE_EXTENSIONS for f in files):
        return "unpacked_script", len(files), [], sample_files

    return "unrecognized", len(files), [], sample_files


def scan_broken_mods(config: Config, conn: sqlite3.Connection) -> list[BrokenModFolder]:
    """Scans Mods/ for unmanaged folders with no loadable .package/.ts4script.

    Mirrors scanner.import_untracked_mods()'s notion of "unmanaged": a real
    (non-symlink) directory not already tracked as a known mod id. A folder
    that does contain a .package/.ts4script is importable and therefore not
    reported here, even if it hasn't been imported yet.
    """
    if not config.sims4_mods_dir.exists():
        return []

    known_ids = {row["id"] for row in conn.execute("SELECT id FROM mods")}
    results = []

    for entry in sorted(config.sims4_mods_dir.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name in known_ids:
            continue
        has_tracked_file = any(
            f.is_file() and f.suffix.lower() in {".package", ".ts4script"} for f in entry.rglob("*")
        )
        if has_tracked_file:
            continue
        reason, file_count, zip_names, sample_files = _classify(entry)
        results.append(
            BrokenModFolder(
                name=entry.name,
                reason=reason,
                file_count=file_count,
                zip_names=zip_names,
                sample_files=sample_files,
            )
        )

    return results


def fix_broken_mod(name: str, config: Config, conn: sqlite3.Connection) -> str | None:
    """Applies the automatic fix for one folder previously reported by
    scan_broken_mods(), if its current reason is one of FIXABLE_REASONS.

    Returns the newly installed mod's id for 'unextracted_archive', or None
    for 'empty' (nothing left to reference). Re-classifies the folder from
    scratch rather than trusting a caller-supplied reason, since the
    filesystem may have changed since the folder was last scanned.
    """
    entry = config.sims4_mods_dir / name
    if not entry.is_dir() or entry.is_symlink():
        raise BrokenModFixError(f"No such unmanaged folder under Mods/: {name}")

    reason, _file_count, zip_names, _sample_files = _classify(entry)

    if reason == "empty":
        backup_folder(entry, name, config)
        shutil.rmtree(entry)
        return None

    if reason == "unextracted_archive":
        if len(zip_names) != 1:
            raise BrokenModFixError(
                f"'{name}' contains {len(zip_names)} archives — pick one to extract manually"
            )
        backup_folder(entry, name, config)
        zip_path = entry / zip_names[0]
        mod_id = mod_manager.install(zip_path, config=config, conn=conn, mod_name=name)
        shutil.rmtree(entry)
        return mod_id

    raise BrokenModFixError(f"No automatic fix available for '{name}' (reason: {reason})")


def delete_broken_folder(name: str, config: Config) -> None:
    """Manual "just get rid of this" action, available for any reason —
    unlike fix_broken_mod()/attempt_script_repair(), this never tries to
    recover anything from the folder's contents, it just removes it. Backed
    up first, same as every other destructive action here, so it's still
    recoverable if the folder actually held something worth keeping.
    """
    entry = config.sims4_mods_dir / name
    if not entry.is_dir() or entry.is_symlink():
        raise BrokenModFixError(f"No such unmanaged folder under Mods/: {name}")

    backup_folder(entry, name, config)
    shutil.rmtree(entry)


def attempt_script_repair(name: str, config: Config, conn: sqlite3.Connection) -> str:
    """Best-effort-only repair for 'unpacked_script', deliberately kept out
    of FIXABLE_REASONS/fix_broken_mod(): a .ts4script is literally a zip
    archive, so re-zipping the folder's own contents (package directories
    preserved at the archive's root, exactly as they sit on disk) *should*
    reconstruct a working mod — but only if the original extraction was
    complete and nothing's been added/removed/modified since, neither of
    which can be verified from here. A folder that was only partially
    extracted, or a package whose original .ts4script had a different
    internal layout, will still produce a zip that installs "successfully"
    while doing nothing in-game. Always backed up first, same as the safe
    fixes above, so a failed attempt is recoverable.
    """
    entry = config.sims4_mods_dir / name
    if not entry.is_dir() or entry.is_symlink():
        raise BrokenModFixError(f"No such unmanaged folder under Mods/: {name}")

    reason, _file_count, _zip_names, _sample_files = _classify(entry)
    if reason != "unpacked_script":
        raise BrokenModFixError(f"'{name}' isn't an extracted-script folder (reason: {reason})")

    backup_folder(entry, name, config)
    with tempfile.TemporaryDirectory(prefix="simslink-script-repair-") as tmp:
        rebuilt = Path(tmp) / f"{name}.ts4script"
        with zipfile.ZipFile(rebuilt, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(entry.rglob("*")):
                if file.is_file():
                    archive.write(file, arcname=file.relative_to(entry).as_posix())
        mod_id = mod_manager.install(rebuilt, config=config, conn=conn, mod_name=name)
    shutil.rmtree(entry)
    return mod_id
