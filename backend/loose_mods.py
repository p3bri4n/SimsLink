"""Adopts .package/.ts4script files sitting loose directly at Mods/ root —
no folder of their own, no archive, just dropped in by hand — into managed,
individually-tracked mods, and offers a confirmable way to tidy up the
result afterward.

This is a *very* common real-world state, not an edge case: the official
install instructions for The Sims 4 ("place .package/.ts4script files in
Mods, no folder needed") teach exactly this, so a library that predates
SimsLink (or that mixes manual installs with app-managed ones) can easily
have hundreds of these. Neither of the other two Mods/-root scanners sees
them at all: scanner.import_untracked_mods() and broken_mods.scan_broken_mods()
both only look at *directories* directly under Mods/, explicitly skipping
anything that isn't one — a loose file is invisible to both.

import_loose_files() is deliberately manual-only (a Settings button, same
as "Full scan"), never folded into the automatic watcher rescan: unlike a
whole folder appearing (a strong, deliberate signal), a single new file at
Mods/ root is ambiguous — it could be a download still in progress, a
misplaced non-mod file, anything. Each imported file becomes its own mod
(no attempt to guess which loose files "really" belong together at import
time — see suggest_groupings()'s docstring for why that's a separate,
confirmable step instead) and is tagged `is_loose_import` so it's easy to
find and easy to tell apart from a normally-installed mod in the UI (the
"Vrac"/"Loose" tag).

suggest_groupings()/merge_mods() are the confirmable follow-up, from two
signal sources (strongest first): mods curseforge_match.py has already
linked to the exact same curseforge_id are a *confirmed* identity, not a
guess — grouped first. Whatever's left is then clustered by a shared
leading-name-segments heuristic (the backend counterpart to app.js's
inferred-author clustering, same idea, simplified) — e.g. five
...package files from the same creator/set. Either way, nothing is ever
merged automatically — the user reviews each suggestion and explicitly
confirms before merge_mods() moves anything. Scoped to only mods still
tagged `is_loose_import`, since a mod that's already been organized
(renamed, merged, or installed normally) isn't this tool's concern
anymore.

The curseforge_id signal is only as trustworthy as the matching data
behind it — see CLAUDE.md's "Data-integrity incident" note (2026-08-23):
a batch of loose mods was once found sharing a wrong curseforge_id from a
now-fixed matcher bug, cleaned up directly in the database. suggest_
groupings() reads the live `mods` table on every call rather than caching
anything, so a repeat of that incident wouldn't get baked into a stale
suggestion — but it also means this function has no way to tell a
currently-good match from a bad one on its own; it just trusts the column.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import mod_manager
from .backups import backup_folder
from .config import Config

logger = logging.getLogger(__name__)

_SEGMENT_RE = re.compile(r"[_\s-]+")
_MIN_PREFIX_CHARS = 4
_MAX_PREFIX_SEGMENTS = 3


class LooseModsError(Exception):
    """Raised by merge_mods() for an invalid request — see its docstring."""


def import_loose_files(config: Config, conn: sqlite3.Connection) -> list[str]:
    """Adopts every loose .package/.ts4script file directly at Mods/ root,
    each becoming its own individually-tracked mod (never guessing which
    ones belong together — see this module's docstring). Returns the new
    mod ids. Non-mod loose files (readmes, screenshots, logs, ...) are left
    untouched, same as mod_manager.walk_mod_files() already ignores them
    inside a real install.
    """
    if not config.sims4_mods_dir.exists():
        return []

    imported: list[str] = []
    for entry in sorted(config.sims4_mods_dir.iterdir()):
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.suffix.lower() not in mod_manager.TRACKED_EXTENSIONS:
            continue
        try:
            mod_id = mod_manager.install(entry, config=config, conn=conn, mod_name=entry.stem)
        except mod_manager.ModManagerError:
            logger.warning("Skipping unimportable loose file at Mods/ root: %s", entry, exc_info=True)
            continue
        conn.execute("UPDATE mods SET is_loose_import = 1 WHERE id = ?", (mod_id,))
        conn.commit()
        # install() only ever copies from its source — the original loose
        # file is still sitting at Mods/ root until removed here. Left in
        # place, the game would load the same content twice (once from the
        # loose file, once from the new managed symlink), and every import
        # would show up as a spurious duplicate_package conflict.
        entry.unlink()
        imported.append(mod_id)

    return imported


@dataclass(frozen=True)
class GroupSuggestion:
    suggested_name: str
    mod_ids: list[str]
    mod_names: list[str]
    curseforge_id: int | None = None  # set only for a confirmed-identity group (shared
    # curseforge_id) — None for a name-heuristic guess, so the frontend can tell the two
    # signal strengths apart instead of presenting every suggestion the same way.


def _leading_segments(name: str, segment_count: int) -> tuple[str, str] | None:
    """Returns (case-insensitive key, display text) for `name`'s first
    `segment_count` segments (split on whitespace/underscore/hyphen), or
    None if the name doesn't have more segments than that (nothing shorter
    left to compare against) or the resulting prefix is too short to be a
    meaningful signal on its own."""
    parts = [p for p in _SEGMENT_RE.split(name.strip()) if p]
    if len(parts) <= segment_count:
        return None
    display = " ".join(parts[:segment_count])
    if len(display) < _MIN_PREFIX_CHARS:
        return None
    return display.lower(), display


def suggest_groupings(conn: sqlite3.Connection) -> list[GroupSuggestion]:
    """Clusters still-loose-tagged mods, strongest signal first:

    1. Mods sharing the exact same curseforge_id (set by curseforge_match.py
       when a loose file's fingerprint exactly matched a real CurseForge
       file) are a confirmed identity — these are the same mod, not a
       guess. Grouped first and removed from consideration below, so a mod
       is never suggested twice under two different signals.
    2. Whatever's left is clustered by shared leading name segments — the
       longest shared prefix (up to _MAX_PREFIX_SEGMENTS) wins first, and
       whatever's left over is retried at shorter prefixes, so e.g. five
       "serenity_x_caio_<Item>.package" files cluster on their full shared
       "serenity x caio" root before anything falls back to a weaker,
       shorter match.

    Purely informational — computed fresh on every call, nothing stored,
    nothing merged; see merge_mods() for the confirmable action.
    """
    rows = conn.execute("SELECT id, name, curseforge_id FROM mods WHERE is_loose_import = 1").fetchall()
    remaining = {row["id"]: row["name"] for row in rows}
    suggestions: list[GroupSuggestion] = []

    by_curseforge_id: dict[int, list[str]] = {}
    for row in rows:
        if row["curseforge_id"] is not None:
            by_curseforge_id.setdefault(row["curseforge_id"], []).append(row["id"])

    for curseforge_id in sorted(by_curseforge_id):
        ids = sorted(by_curseforge_id[curseforge_id])
        if len(ids) < 2:
            continue
        names = [remaining[mod_id] for mod_id in ids]
        suggestions.append(
            GroupSuggestion(
                suggested_name=max(names, key=len),  # the most descriptive name, as a starting point
                mod_ids=ids,
                mod_names=names,
                curseforge_id=curseforge_id,
            )
        )
        for mod_id in ids:
            remaining.pop(mod_id, None)

    for segment_count in range(_MAX_PREFIX_SEGMENTS, 0, -1):
        clusters: dict[str, list[tuple[str, str]]] = {}
        for mod_id, name in remaining.items():
            result = _leading_segments(name, segment_count)
            if result is None:
                continue
            key, display = result
            clusters.setdefault(key, []).append((mod_id, display))

        for entries in clusters.values():
            if len(entries) < 2:
                continue
            ids = sorted(mod_id for mod_id, _ in entries)
            suggestions.append(
                GroupSuggestion(
                    suggested_name=entries[0][1],
                    mod_ids=ids,
                    mod_names=[remaining[mod_id] for mod_id in ids],
                )
            )
            for mod_id in ids:
                remaining.pop(mod_id, None)

    return suggestions


def merge_mods(mod_ids: list[str], new_name: str, *, config: Config, conn: sqlite3.Connection) -> str:
    """Combines several loose-imported mods into one, under `new_name`.
    Only ever reachable from a confirm-modal-gated frontend action — never
    automatic. Scoped to `is_loose_import` mods specifically (this is a
    tidy-up tool for suggest_groupings()' output, not a general "merge any
    two mods" feature — dependency/profile references on an ordinary
    installed mod aren't accounted for here).

    Backs up each source mod's folder first (backups.py, same as every
    other rearranging action in this app), stages every source file into
    one archive, and installs that archive as a single new mod through the
    normal pipeline — then deletes the old mods. A collision between two
    sources' filenames is disambiguated by suffixing the source mod's id,
    rather than one silently overwriting the other.

    If every source shares the same curseforge_id (a suggest_groupings()
    confirmed-identity group — see there), the new merged mod inherits that
    id plus the author/category/short_description/thumbnail_url/links that
    came with it, so the result stays "Linked" instead of reverting to
    unlinked. A plain name-heuristic group (curseforge_id all NULL, or
    disagreeing) gets none of this — install() already left the new mod
    with sensible blank defaults for that case.
    """
    if len(mod_ids) < 2:
        raise LooseModsError("Select at least 2 mods to merge")

    rows = []
    for mod_id in mod_ids:
        row = conn.execute("SELECT * FROM mods WHERE id = ?", (mod_id,)).fetchone()
        if row is None:
            raise LooseModsError(f"No such mod: {mod_id}")
        if not row["is_loose_import"]:
            raise LooseModsError(
                f"'{row['name']}' wasn't imported from a loose file — merge is only for those"
            )
        rows.append(row)

    for row in rows:
        backup_folder(Path(row["library_path"]), row["id"], config)

    with tempfile.TemporaryDirectory(prefix="simslink-merge-") as tmp:
        archive_path = Path(tmp) / "merged.zip"
        used_names: set[str] = set()
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for row in rows:
                library_path = Path(row["library_path"])
                for f in library_path.iterdir():
                    if not f.is_file():
                        continue
                    dest_name = f.name
                    if dest_name in used_names:
                        dest_name = f"{f.stem}-{row['id']}{f.suffix}"
                    used_names.add(dest_name)
                    archive.write(f, arcname=dest_name)

        new_mod_id = mod_manager.install(archive_path, config=config, conn=conn, mod_name=new_name)

    shared_curseforge_ids = {row["curseforge_id"] for row in rows}
    if len(shared_curseforge_ids) == 1 and (shared_curseforge_id := next(iter(shared_curseforge_ids))) is not None:
        source = rows[0]
        conn.execute(
            "UPDATE mods SET curseforge_id = ?, author = ?, category = ?, short_description = ?, "
            "thumbnail_url = ?, links = ? WHERE id = ?",
            (
                shared_curseforge_id,
                source["author"],
                source["category"],
                source["short_description"],
                source["thumbnail_url"],
                source["links"],
                new_mod_id,
            ),
        )
        conn.commit()

    for mod_id in mod_ids:
        mod_manager.delete(mod_id, config=config, conn=conn)

    return new_mod_id
