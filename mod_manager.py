"""Mod install / enable / disable / delete pipeline.

Enforces the game's placement rule everywhere a mod folder is written: every
mod lives at Mods/<mod_id>/, .package files may sit at any depth under it,
but .ts4script files are always flattened to sit directly inside it — nesting
one level further would put them two levels under Mods/, which the game
silently ignores. See CLAUDE.md's "Critical game constraints".
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from config import Config

TRACKED_EXTENSIONS = {".package", ".ts4script"}
_HASH_CHUNK_SIZE = 1024 * 1024
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ModManagerError(Exception):
    """Raised for install/enable/disable/delete failures."""


@dataclass(frozen=True)
class ModFile:
    relative_path: Path
    extension: str


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk_mod_files(root: Path) -> list[ModFile]:
    """Lists every .package/.ts4script under root; other files (readmes,
    screenshots, ...) are ignored — they're not something the game loads."""
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in TRACKED_EXTENSIONS:
            files.append(
                ModFile(relative_path=path.relative_to(root), extension=path.suffix.lower())
            )
    return files


def generate_unique_mod_id(hint: str, conn: sqlite3.Connection) -> str:
    base = _SLUG_RE.sub("-", hint.lower()).strip("-") or "mod"
    candidate = base
    suffix = 2
    while conn.execute("SELECT 1 FROM mods WHERE id = ?", (candidate,)).fetchone():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


@contextmanager
def _stage_source(source: Path) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="simslink-install-") as tmp:
        staged = Path(tmp)
        suffix = source.suffix.lower()
        if suffix == ".zip":
            with zipfile.ZipFile(source) as archive:
                archive.extractall(staged)
        elif suffix in TRACKED_EXTENSIONS:
            shutil.copy2(source, staged / source.name)
        else:
            raise ModManagerError(f"Unsupported source file type: {source.suffix}")
        yield staged


def _resolve_destination(mod_file: ModFile) -> Path:
    if mod_file.extension == ".ts4script":
        return Path(mod_file.relative_path.name)
    return mod_file.relative_path


def _copy_mod_files(
    source_dir: Path, library_path: Path, files: list[ModFile]
) -> dict[ModFile, Path]:
    destinations: dict[ModFile, Path] = {}
    used: set[Path] = set()
    for mod_file in files:
        dest_rel = _resolve_destination(mod_file)
        if dest_rel in used:
            raise ModManagerError(
                "Filename collision after flattening .ts4script files to the "
                f"mod root: {dest_rel}"
            )
        used.add(dest_rel)
        dest_abs = library_path / dest_rel
        dest_abs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_dir / mod_file.relative_path, dest_abs)
        destinations[mod_file] = dest_rel
    return destinations


def _determine_primary_type(files: list[ModFile]) -> str:
    extensions = {f.extension for f in files}
    if ".package" in extensions and ".ts4script" in extensions:
        return "mixed"
    if ".ts4script" in extensions:
        return "script"
    return "package"


def _insert_mod_files(
    conn: sqlite3.Connection,
    mod_id: str,
    library_path: Path,
    destinations: dict[ModFile, Path],
) -> None:
    for mod_file, dest_rel in destinations.items():
        abs_path = library_path / dest_rel
        stat = abs_path.stat()
        conn.execute(
            "INSERT INTO mod_files (mod_id, relative_path, hash, extension, size, mtime) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                mod_id,
                dest_rel.as_posix(),
                hash_file(abs_path),
                mod_file.extension,
                stat.st_size,
                stat.st_mtime,
            ),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finalize_install(
    mod_id: str,
    source_dir: Path,
    files: list[ModFile],
    name: str,
    config: Config,
    conn: sqlite3.Connection,
    *,
    after_copy: Callable[[], None] | None = None,
) -> str:
    library_path = config.library_dir / mod_id
    library_path.mkdir(parents=True)
    try:
        destinations = _copy_mod_files(source_dir, library_path, files)
        primary_type = _determine_primary_type(files)
        conn.execute(
            "INSERT INTO mods (id, name, library_path, primary_type, install_date, active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (mod_id, name, str(library_path), primary_type, _now_iso()),
        )
        _insert_mod_files(conn, mod_id, library_path, destinations)
        if after_copy is not None:
            after_copy()
        _activate(mod_id, config)
    except Exception:
        conn.rollback()
        shutil.rmtree(library_path, ignore_errors=True)
        raise
    conn.commit()
    return mod_id


def install(
    source: Path,
    *,
    config: Config,
    conn: sqlite3.Connection,
    mod_name: str | None = None,
) -> str:
    """Installs a mod from a .zip archive or a bare .package/.ts4script file.

    Returns the new mod's id.
    """
    with _stage_source(source) as staged_dir:
        files = walk_mod_files(staged_dir)
        if not files:
            raise ModManagerError(f"No .package or .ts4script files found in {source}")
        name = mod_name or source.stem
        mod_id = generate_unique_mod_id(mod_name or source.stem, conn)
        return _finalize_install(mod_id, staged_dir, files, name, config, conn)


def import_existing_folder(source_dir: Path, *, config: Config, conn: sqlite3.Connection) -> str:
    """Adopts a mod folder already sitting directly under Mods/ (pre-dating
    SimsLink) into the managed library, then replaces it with a symlink."""
    files = walk_mod_files(source_dir)
    if not files:
        raise ModManagerError(f"No .package or .ts4script files found in {source_dir}")
    mod_id = generate_unique_mod_id(source_dir.name, conn)
    return _finalize_install(
        mod_id,
        source_dir,
        files,
        source_dir.name,
        config,
        conn,
        after_copy=lambda: shutil.rmtree(source_dir),
    )


def _get_mod(conn: sqlite3.Connection, mod_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM mods WHERE id = ?", (mod_id,)).fetchone()
    if row is None:
        raise ModManagerError(f"No such mod: {mod_id}")
    return row


def _activate(mod_id: str, config: Config) -> None:
    library_path = config.library_dir / mod_id
    link_path = config.sims4_mods_dir / mod_id
    if link_path.exists() or link_path.is_symlink():
        return
    config.sims4_mods_dir.mkdir(parents=True, exist_ok=True)
    if config.symlink_support:
        link_path.symlink_to(library_path, target_is_directory=True)
    else:
        shutil.copytree(library_path, link_path)


def _deactivate(mod_id: str, config: Config) -> None:
    link_path = config.sims4_mods_dir / mod_id
    if link_path.is_symlink():
        link_path.unlink()
    elif link_path.is_dir():
        shutil.rmtree(link_path)


def enable(mod_id: str, *, config: Config, conn: sqlite3.Connection) -> None:
    _get_mod(conn, mod_id)
    _activate(mod_id, config)
    conn.execute("UPDATE mods SET active = 1 WHERE id = ?", (mod_id,))
    conn.commit()


def disable(mod_id: str, *, config: Config, conn: sqlite3.Connection) -> None:
    _get_mod(conn, mod_id)
    _deactivate(mod_id, config)
    conn.execute("UPDATE mods SET active = 0 WHERE id = ?", (mod_id,))
    conn.commit()


def delete(mod_id: str, *, config: Config, conn: sqlite3.Connection) -> None:
    row = _get_mod(conn, mod_id)
    _deactivate(mod_id, config)
    library_path = Path(row["library_path"])
    if library_path.exists():
        shutil.rmtree(library_path)
    conn.execute("DELETE FROM mods WHERE id = ?", (mod_id,))
    conn.commit()
