"""Duplicate/conflict detection across the installed mod library.

Four signals:

  - Exact duplicate mods: two mods whose *entire* file sets are byte-
    identical (same set of hashes, not just one shared file) — the
    strongest signal here, since there's no plausible reading of "shares
    100% of its files with another mod" other than "this is the same mod
    twice." Suppresses every other signal below for any pair it covers.
    Deciding *which* of the pair to call "the duplicate" for display
    purposes is main.py's job (it has each mod's `author`), not this
    module's — see its /api/conflicts route.
  - .package duplicates: two or more mods ship a byte-identical file (same
    hash), derived entirely from data already tracked in mod_files (no new
    file parsing — every file's hash is already computed at install time by
    mod_manager.py, kept current by scanner.py). Usually the same mod
    installed twice under different names, or one mod bundling another's
    file wholesale.
  - .ts4script name collisions: two or more mods ship a script file with the
    same filename at their mod root (.ts4script files are always flattened
    to the mod root — see mod_manager.py). Since .ts4script archives are
    Python zipimport archives and the interpreter's module cache keys on
    module name, not file path, two mods shipping a same-named script —
    often each bundling their own copy of a shared library like
    sims4communitylib — can silently shadow each other: a well-documented
    source of confusing bugs in the modding community.
  - Folder duplication: two or more mods whose *name* is identical except for
    a trailing "(1)"/"(2)"/... — the classic signature a file
    manager/browser leaves when a file/folder is downloaded or dropped in
    twice rather than overwritten. Name-only, no file data needed, and by
    far the strongest of the three signals (a real observed case: the
    generic byte-duplicate and script-collision checks above independently
    flagged the exact same pair for this exact reason). Suppresses the
    other two kinds for any pair it already covers, rather than reporting
    the same underlying issue three different, differently-worded ways.

Purely informational: this never blocks install/enable and never suggests
deleting anything — same "suspicion is not confirmation" spirit as
crash_analyzer.py's suspects (CLAUDE.md's "Things to never do"). Even the
"strongest" signal above is still just a naming pattern, not a guarantee —
never auto-resolved.

Only ever considers currently-*active* mods. A disabled mod's files aren't
loaded by the game at all, so it can't actually collide with anything right
now — disabling one side of a conflict is itself a valid resolution, and
should make the conflict (and the other mod's "problem" highlight) disappear
entirely, not just fall silent about the disabled mod's own card.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ConflictGroup:
    kind: str  # 'exact_duplicate_mod' | 'duplicate_package' | 'ts4script_name_collision' | 'folder_duplication'
    identifier: str  # the shared hash/filename, or a file count (see file_count)
    mod_ids: list[str]
    file_count: int = 1  # >1 for duplicate_package/exact_duplicate_mod: how many distinct shared files


def _mod_ids_sharing(conn: sqlite3.Connection, *, where: str, params: tuple) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT mf.mod_id FROM mod_files mf JOIN mods m ON m.id = mf.mod_id "
        f"WHERE m.active = 1 AND {where} ORDER BY mf.mod_id",
        params,
    )
    return [row["mod_id"] for row in rows]


def find_exact_duplicate_mods(conn: sqlite3.Connection) -> list[ConflictGroup]:
    # Every active mod's complete set of file hashes, grouped by that exact
    # set (order doesn't matter — frozenset — and a mod with zero hashed
    # files, e.g. one that hasn't been scanned yet, is excluded rather than
    # matching every other equally-empty mod). Two or more mods landing on
    # the same set share 100% of their files with each other, not just one
    # coincidental file, unlike find_package_duplicates() below.
    rows = conn.execute(
        "SELECT mf.mod_id, mf.hash FROM mod_files mf JOIN mods m ON m.id = mf.mod_id "
        "WHERE m.active = 1 AND mf.hash IS NOT NULL"
    ).fetchall()

    hashes_by_mod: dict[str, set[str]] = {}
    for row in rows:
        hashes_by_mod.setdefault(row["mod_id"], set()).add(row["hash"])

    mod_ids_by_signature: dict[frozenset, list[str]] = {}
    for mod_id, hashes in hashes_by_mod.items():
        if not hashes:
            continue
        mod_ids_by_signature.setdefault(frozenset(hashes), []).append(mod_id)

    return sorted(
        (
            ConflictGroup(
                kind="exact_duplicate_mod",
                identifier=str(len(signature)),
                mod_ids=sorted(mod_ids),
                file_count=len(signature),
            )
            for signature, mod_ids in mod_ids_by_signature.items()
            if len(mod_ids) > 1
        ),
        key=lambda g: g.mod_ids,
    )


def find_package_duplicates(conn: sqlite3.Connection) -> list[ConflictGroup]:
    # Grouped by the *pair* of mods sharing files, not by individual hash —
    # two mods bundling dozens of the same override files (a real, observed
    # case) would otherwise produce dozens of near-identical rows differing
    # only by an opaque hash, reading as a rendering bug rather than a
    # meaningful signal. One row per pair, with file_count showing how many
    # distinct files they share.
    hash_rows = conn.execute(
        "SELECT GROUP_CONCAT(DISTINCT mf.mod_id) AS mod_ids FROM mod_files mf "
        "JOIN mods m ON m.id = mf.mod_id "
        "WHERE mf.extension = '.package' AND mf.hash IS NOT NULL AND m.active = 1 "
        "GROUP BY mf.hash HAVING COUNT(DISTINCT mf.mod_id) > 1"
    ).fetchall()

    file_counts: dict[tuple[str, ...], int] = {}
    for row in hash_rows:
        mod_ids = tuple(sorted(row["mod_ids"].split(",")))
        file_counts[mod_ids] = file_counts.get(mod_ids, 0) + 1

    return [
        ConflictGroup(
            kind="duplicate_package",
            identifier=str(file_count),
            mod_ids=list(mod_ids),
            file_count=file_count,
        )
        for mod_ids, file_count in sorted(file_counts.items())
    ]


def find_ts4script_name_collisions(conn: sqlite3.Connection) -> list[ConflictGroup]:
    name_rows = conn.execute(
        "SELECT mf.relative_path FROM mod_files mf "
        "JOIN mods m ON m.id = mf.mod_id "
        "WHERE mf.extension = '.ts4script' AND m.active = 1 "
        "GROUP BY mf.relative_path HAVING COUNT(DISTINCT mf.mod_id) > 1"
    ).fetchall()
    return [
        ConflictGroup(
            kind="ts4script_name_collision",
            identifier=row["relative_path"],
            mod_ids=_mod_ids_sharing(
                conn, where="mf.relative_path = ? AND mf.extension = '.ts4script'", params=(row["relative_path"],)
            ),
        )
        for row in name_rows
    ]


_TRAILING_COUNTER_RE = re.compile(r"\s*\(\d+\)$")


def find_folder_duplications(conn: sqlite3.Connection) -> list[ConflictGroup]:
    rows = conn.execute("SELECT id, name FROM mods WHERE active = 1").fetchall()

    mod_ids_by_key: dict[str, list[str]] = {}
    display_name_by_key: dict[str, str] = {}
    any_suffix_stripped: dict[str, bool] = {}
    for row in rows:
        base_name = _TRAILING_COUNTER_RE.sub("", row["name"]).strip()
        key = base_name.lower()
        mod_ids_by_key.setdefault(key, []).append(row["id"])
        display_name_by_key.setdefault(key, base_name)
        if base_name != row["name"]:
            any_suffix_stripped[key] = True

    return sorted(
        (
            ConflictGroup(kind="folder_duplication", identifier=display_name_by_key[key], mod_ids=sorted(mod_ids))
            for key, mod_ids in mod_ids_by_key.items()
            if len(mod_ids) > 1 and any_suffix_stripped.get(key)
        ),
        key=lambda g: g.identifier,
    )


def find_conflicts(conn: sqlite3.Connection) -> list[ConflictGroup]:
    # Precedence, strongest first: an exact full-file-set match beats a mere
    # name pattern (folder_duplication), which in turn beats the two
    # generic single-file/single-name signals — each stronger signal
    # suppresses the weaker ones for any pair it already covers, so the
    # same underlying issue is never reported multiple different,
    # differently-worded ways.
    exact_duplicates = find_exact_duplicate_mods(conn)
    covered_pairs = [frozenset(group.mod_ids) for group in exact_duplicates]

    def already_covered(group: ConflictGroup) -> bool:
        return any(frozenset(group.mod_ids) <= covered for covered in covered_pairs)

    folder_duplications = [group for group in find_folder_duplications(conn) if not already_covered(group)]
    covered_pairs += [frozenset(group.mod_ids) for group in folder_duplications]

    other_conflicts = [
        group
        for group in find_package_duplicates(conn) + find_ts4script_name_collisions(conn)
        if not already_covered(group)
    ]
    return exact_duplicates + folder_duplications + other_conflicts
