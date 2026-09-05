"""Detects folders directly under Mods/ that don't contain anything the game
would actually load, and classifies *why* on a best-effort basis.

This looks at the same population as scanner.import_untracked_mods() (real,
non-symlink directories directly under Mods/ that aren't a known managed mod
id). scan_broken_mods() is a read-only diagnostic, surfaced alongside
conflict_detector.py's findings in the Library warnings banner.

fix_broken_mod() applies a fix for the two reasons safe enough to automate
without asking anything further — 'empty' (delete) and 'unextracted_archive'
with exactly one unambiguous archive (extract via the normal install
pipeline) — but only when the frontend calls it after the user explicitly
confirms (same confirm-modal pattern as cache_cleaner.py and mod deletion),
never on its own. extract_selected_zips() handles 'unextracted_archive' with
two or more archives, where the user picks which one(s) — see its own
docstring for why this isn't just a variant of fix_broken_mod().
attempt_script_repair() covers 'unpacked_script' separately, and is
explicitly *not* one of the safe fixes above — it's a best-effort re-zip
that can produce a mod that "installs" but still doesn't load in-game (see
its own docstring). 'unrecognized' has no automatic action of any kind: it
isn't even confidently a mod in the first place. Same "suspicion is not
confirmation" spirit as everywhere else in this project.

Before either fix touches the filesystem, the folder is backed up via
backups.py (the same LIBRARY_DIR/.backups/ mechanism download_watcher.py
uses before a replace) — an automatic fix acting on data it didn't create
should be reversible, same as every other destructive action in this app.

scan_rezipped_mods()/fix_rezipped_mod() cover a related but distinct
population: an *already-tracked* mod whose real library folder stopped
containing anything loadable. `Mods/<mod_id>/` is a symlink straight into
that library folder, so a manual edit made there (e.g. re-zipping the
mod's extracted files back into an archive, directly in the game's Mods/
folder) lands in the library folder itself — this is a real-world case
that came up during manual testing ("dezip a mod via the app, then rezip
it manually"). Unlike the folders above, this one already has a DB row
(and stale mod_files rows, until the next rescan catches up), so it's
reported and fixed separately rather than folded into scan_broken_mods().

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
    # Paths relative to the folder (posix-style, e.g. "Optional/Variant.zip")
    # — a zip can sit at any depth (_classify() scans recursively), so a bare
    # filename isn't enough to find it again; this used to store just names,
    # which broke extraction for anything not sitting at the folder's root.
    zip_paths: list[str] = field(default_factory=list)
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

    zip_paths = [f.relative_to(entry).as_posix() for f in files if f.suffix.lower() == ".zip"]
    if zip_paths:
        return "unextracted_archive", len(files), sorted(zip_paths), sample_files

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
        reason, file_count, zip_paths, sample_files = _classify(entry)
        results.append(
            BrokenModFolder(
                name=entry.name,
                reason=reason,
                file_count=file_count,
                zip_paths=zip_paths,
                sample_files=sample_files,
            )
        )

    return results


def fix_broken_mod(name: str, config: Config, conn: sqlite3.Connection) -> str | None:
    """Applies the automatic fix for one folder previously reported by
    scan_broken_mods(), if its current reason is one of FIXABLE_REASONS.

    Returns the newly installed mod's id for 'unextracted_archive', or None
    for 'empty' (nothing left to reference) or when the sole archive turned
    out to need another round of extract_selected_zips() (see its
    docstring). Re-classifies the folder from scratch rather than trusting a
    caller-supplied reason, since the filesystem may have changed since the
    folder was last scanned.
    """
    entry = config.sims4_mods_dir / name
    if not entry.is_dir() or entry.is_symlink():
        raise BrokenModFixError(f"No such unmanaged folder under Mods/: {name}")

    reason, _file_count, zip_paths, _sample_files = _classify(entry)

    if reason == "empty":
        backup_folder(entry, name, config)
        shutil.rmtree(entry)
        return None

    if reason == "unextracted_archive":
        if len(zip_paths) != 1:
            raise BrokenModFixError(f"'{name}' contains {len(zip_paths)} archives — pick which to extract")
        result = extract_selected_zips(name, config, conn, zip_paths)
        return result["installed"][0] if result["installed"] else None

    raise BrokenModFixError(f"No automatic fix available for '{name}' (reason: {reason})")


def extract_selected_zips(
    name: str, config: Config, conn: sqlite3.Connection, zip_paths: list[str]
) -> dict[str, list[str]]:
    """Extracts one or more archives out of an 'unextracted_archive' folder,
    each becoming its own installed mod. Handles the common real-world case
    fix_broken_mod() can't: a single download bundling several archives the
    user has to choose between (mutually exclusive variants, e.g. different
    languages) or all needs (required pieces split across files) — "often we
    have to choose one or several."

    Each selected archive installs under a name that disambiguates it from
    its siblings (folder name + that archive's own filename stem) when more
    than one is selected, so two selections from the same folder don't
    collide. The whole original folder — including any archive the user did
    *not* select — is backed up once before anything is extracted, then
    removed entirely once every selected archive has been handled; an
    unselected archive is still recoverable from that backup afterward.

    An archive that itself contains only further archives (no
    .package/.ts4script of its own — "another level of zip") isn't
    recursed into here: it's extracted into a fresh folder directly under
    Mods/ instead, so the next scan_broken_mods() reports *that* as a new
    'unextracted_archive' entry with its own zip_paths — the same
    choose-what-to-extract flow just runs again one level deeper, rather
    than this needing bespoke unlimited-depth recursion. Returned under
    "deferred" (folder names, not mod ids) so the caller can tell the two
    outcomes apart.
    """
    entry = config.sims4_mods_dir / name
    if not entry.is_dir() or entry.is_symlink():
        raise BrokenModFixError(f"No such unmanaged folder under Mods/: {name}")

    reason, _file_count, available, _sample_files = _classify(entry)
    if reason != "unextracted_archive":
        raise BrokenModFixError(f"'{name}' isn't an unextracted archive (reason: {reason})")
    if not zip_paths:
        raise BrokenModFixError("Select at least one archive to extract")
    invalid = [p for p in zip_paths if p not in available]
    if invalid:
        raise BrokenModFixError(f"'{name}' has no archive(s) at: {', '.join(invalid)}")

    backup_folder(entry, name, config)
    installed: list[str] = []
    deferred: list[str] = []
    for zip_path in zip_paths:
        archive_path = entry / zip_path
        candidate_name = name if len(zip_paths) == 1 else f"{name} - {Path(zip_path).stem}"
        try:
            installed.append(mod_manager.install(archive_path, config=config, conn=conn, mod_name=candidate_name))
        except mod_manager.ModManagerError:
            deferred.append(_extract_to_new_mods_folder(archive_path, candidate_name, config))
    shutil.rmtree(entry)
    return {"installed": installed, "deferred": deferred}


def _extract_to_new_mods_folder(archive_path: Path, hint: str, config: Config) -> str:
    folder_name = _unique_mods_subfolder_name(hint, config)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(config.sims4_mods_dir / folder_name)
    return folder_name


def _unique_mods_subfolder_name(hint: str, config: Config) -> str:
    base = mod_manager.slugify(hint)
    candidate = base
    suffix = 2
    while (config.sims4_mods_dir / candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


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


@dataclass(frozen=True)
class RezippedMod:
    mod_id: str
    name: str
    zip_paths: list[str] = field(default_factory=list)


def scan_rezipped_mods(config: Config, conn: sqlite3.Connection) -> list[RezippedMod]:
    """Finds an already-tracked mod whose real library folder no longer has
    any .package/.ts4script in it, but does have a .zip — see this module's
    docstring for the "rezipped directly in Mods/" scenario this covers.
    Deliberately re-checks the filesystem live rather than trusting
    mod_files, since that table only catches up after the next scan runs.
    """
    results = []
    for row in conn.execute("SELECT id, name, library_path FROM mods"):
        library_path = Path(row["library_path"])
        if not library_path.is_dir():
            continue
        files = [f for f in library_path.rglob("*") if f.is_file()]
        if any(f.suffix.lower() in mod_manager.TRACKED_EXTENSIONS for f in files):
            continue
        zip_paths = sorted(
            f.relative_to(library_path).as_posix() for f in files if f.suffix.lower() == ".zip"
        )
        if not zip_paths:
            continue
        results.append(RezippedMod(mod_id=row["id"], name=row["name"], zip_paths=zip_paths))
    return results


def fix_rezipped_mod(mod_id: str, config: Config, conn: sqlite3.Connection) -> str:
    """Re-extracts a rezipped mod's archive in place: stages a copy of the
    zip outside the folder, backs up the folder, then replaces the mod the
    same way download_watcher.confirm_replace() replaces an update — delete
    the old library folder/DB row, reinstall fresh under the same name —
    so a correct mod_files/primary_type comes out of the normal install
    pipeline instead of hand-rolling extraction here. `generate_unique_mod_id`
    will hand back the same id as before in the common case, since deleting
    the old row frees it up first.

    Only handles the unambiguous case of exactly one zip and zero loadable
    files — if either has changed since the last scan (or there's more than
    one archive to choose between), raises rather than guessing, same
    restraint as fix_broken_mod().
    """
    row = conn.execute("SELECT name, library_path FROM mods WHERE id = ?", (mod_id,)).fetchone()
    if row is None:
        raise BrokenModFixError(f"No such mod: {mod_id}")

    library_path = Path(row["library_path"])
    files = [f for f in library_path.rglob("*") if f.is_file()]
    if any(f.suffix.lower() in mod_manager.TRACKED_EXTENSIONS for f in files):
        raise BrokenModFixError(f"'{mod_id}' already has loadable content")
    zip_paths = [f for f in files if f.suffix.lower() == ".zip"]
    if len(zip_paths) != 1:
        raise BrokenModFixError(
            f"'{mod_id}' contains {len(zip_paths)} archives — pick which to extract"
        )

    with tempfile.TemporaryDirectory(prefix="simslink-rezip-fix-") as tmp:
        staged_zip = Path(tmp) / zip_paths[0].name
        shutil.copy2(zip_paths[0], staged_zip)
        backup_folder(library_path, mod_id, config)
        mod_manager.delete(mod_id, config=config, conn=conn)
        return mod_manager.install(staged_zip, config=config, conn=conn, mod_name=row["name"])
