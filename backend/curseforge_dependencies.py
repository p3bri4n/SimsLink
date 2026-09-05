"""Refreshes a linked mod's CurseForge-sourced data — declared dependencies
and game-version compatibility — from its main file (Direct Mode only).

Mirrors curseforge_match.py's role: the bridge between curseforge.py (the
only module allowed to require CURSEFORGE_API_KEY) and a mode-independent
module (dependencies.py, which must stay import-safe for mod_manager.py —
see its own module docstring). Nothing here runs automatically; it's only
ever reached from an explicit, on-demand user action — either the detail
panel's "Check CurseForge dependencies" button (one mod), or the header's
"Synchroniser" button (start_sync_session()/run_sync_step(), every linked
mod, chunked and resumable — same shape as curseforge_match.py's own bulk
run) — never a background/automatic scan.

Both halves reuse the exact same client.get_file() response:
compat_status/game_version_min/max come from CurseForgeFile's own fields,
dependencies from CurseForgeFile.dependencies — one request already carries
everything this module needs, no extra API call per concern.

Only RequiredDependency/OptionalDependency are modeled (CurseForge's other
FileRelationType values — EmbeddedLibrary/Tool/Incompatible/Include — aren't
"needs this other mod installed" relations). A declared dependency that
doesn't resolve to an already-installed local mod (no mods.curseforge_id
match) is skipped entirely rather than stored as a placeholder "unknown
mod" row — there'd be nothing useful to show for it without a second
network round-trip just to fetch its name.

Every created dependency row lands as confidence='suggested', never
'confirmed' — same rule dependencies.py's translation detection already
follows. Unlike translation detection's two-step detect-then-suggest dance
(needed there because a translation signal is inherently ambiguous, several
weak candidates), a CurseForge-declared dependency names an exact modId
with nothing left to disambiguate once resolved to a local mod — so this
writes its 'suggested' rows directly in one step.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import requests

from . import curseforge
from . import dependencies

# CurseForge FileRelationType enum — only these two describe "this mod needs
# that other mod installed"; everything else is out of scope for a
# dependency graph (see module docstring).
_RELATION_TYPE_MAP = {2: "optional", 3: "required"}


class CurseForgeDependenciesError(Exception):
    pass


def _apply_game_compat(
    conn: sqlite3.Connection, mod_id: str, cf_file: curseforge.CurseForgeFile, game_version: str | None
) -> None:
    status = curseforge.compat_status(cf_file.game_version_min, cf_file.game_version_max, game_version)
    conn.execute(
        "UPDATE mods SET compat_status = ?, game_version_min = ?, game_version_max = ? WHERE id = ?",
        (status, cf_file.game_version_min, cf_file.game_version_max, mod_id),
    )
    conn.commit()


def fetch_and_suggest_dependencies(
    mod_id: str, *, client, conn: sqlite3.Connection, game_version: str | None = None
) -> list[dependencies.DependencyLink]:
    """For a linked mod (mods.curseforge_id set), fetches its main
    CurseForge file and:
    - updates compat_status/game_version_min/game_version_max against
      `game_version` (skipped when `game_version` is None — the caller
      doesn't always have/want to check it, e.g. the existing test suite's
      dependency-only assertions).
    - creates a 'suggested' dependencies row for each declared dependency
      that resolves to an already-installed local mod. Idempotent — skips a
      (depends_on_mod_id, dependency_type) pair that's already stored,
      confirmed or suggested, so re-running this never duplicates a row.

    Returns the mod's full current dependency list either way (including
    anything unrelated to this call, e.g. a manually confirmed translation
    link)."""
    row = conn.execute("SELECT curseforge_id FROM mods WHERE id = ?", (mod_id,)).fetchone()
    if row is None or row["curseforge_id"] is None:
        raise CurseForgeDependenciesError(f"'{mod_id}' isn't linked to a CurseForge mod")
    curseforge_id = row["curseforge_id"]

    cf_mod = client.get_mod(curseforge_id)
    if cf_mod.main_file_id is not None:
        cf_file = client.get_file(curseforge_id, cf_mod.main_file_id)
        _apply_game_compat(conn, mod_id, cf_file, game_version)

        existing = {
            (link.depends_on_mod_id, link.dependency_type)
            for link in dependencies.list_dependencies(mod_id, conn)
        }
        for dep in cf_file.dependencies:
            dep_type = _RELATION_TYPE_MAP.get(dep.relation_type)
            if dep_type is None or dep.mod_id == curseforge_id:
                continue
            local = conn.execute(
                "SELECT id FROM mods WHERE curseforge_id = ?", (dep.mod_id,)
            ).fetchone()
            if local is None or local["id"] == mod_id or (local["id"], dep_type) in existing:
                continue
            dependencies.add_dependency(
                mod_id,
                conn=conn,
                dependency_type=dep_type,
                depends_on_mod_id=local["id"],
                confidence="suggested",
                mandatory=(dep_type == "required"),
            )

    return dependencies.list_dependencies(mod_id, conn)


# --- bulk sync (every already-linked mod) -------------------------------------------


# Smaller than curseforge_match.CHUNK_SIZE (20): each mod here costs two
# sequential API calls (get_mod + get_file), not one batched pair of calls
# per whole chunk, so a smaller chunk keeps a single step from taking too
# long before the progress popup gets to update.
SYNC_CHUNK_SIZE = 10


@dataclass
class SyncSession:
    """One in-progress bulk sync run over every already-linked mod — held in
    app.state (main.py) between POST .../start and repeated POST .../step
    calls. `remaining` shrinks each step(); `done` is true once nothing's
    left."""

    remaining: list[str] = field(default_factory=list)  # mod ids
    total: int = 0
    checked: int = 0
    synced: int = 0
    errors: int = 0

    @property
    def done(self) -> bool:
        return not self.remaining


def _linked_mod_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT id FROM mods WHERE curseforge_id IS NOT NULL").fetchall()
    return [row["id"] for row in rows]


def start_sync_session(conn: sqlite3.Connection) -> SyncSession:
    """Always builds a fresh candidate list from the DB — every currently-
    linked mod, regardless of whether it's ever been synced before (there's
    no "last synced" bookkeeping; re-running this is always a full pass,
    same simplicity curseforge_match.py's own start_session() already
    accepts for its own candidate population)."""
    candidates = _linked_mod_ids(conn)
    return SyncSession(remaining=candidates, total=len(candidates))


def run_sync_step(
    session: SyncSession,
    conn: sqlite3.Connection,
    client,
    game_version: str | None,
    chunk_size: int = SYNC_CHUNK_SIZE,
) -> SyncSession:
    """Unlike curseforge_match.run_step() — which makes one batched pair of
    API calls per whole chunk, so it can safely leave `session` untouched
    until they succeed — fetch_and_suggest_dependencies() calls the API once
    per mod. So a single mod's transient failure is caught and skipped
    individually here (counted in `errors`, that mod just stays stale until
    a later sync run) rather than aborting the whole chunk over one flaky
    request. A CurseForgeAuthError (the key itself rejected) is not
    retryable and propagates out uncaught, same as curseforge_match.py's own
    handling — the caller (main.py) fails the whole run and clears the
    session rather than treating it as a per-mod skip."""
    chunk = session.remaining[:chunk_size]
    for mod_id in chunk:
        try:
            fetch_and_suggest_dependencies(mod_id, client=client, conn=conn, game_version=game_version)
            session.synced += 1
        except CurseForgeDependenciesError:
            continue  # not linked (shouldn't happen — candidates are pre-filtered), skip
        except curseforge.CurseForgeAuthError:
            raise
        except (curseforge.CurseForgeError, requests.RequestException):
            session.errors += 1
            continue
    session.checked += len(chunk)
    session.remaining = session.remaining[chunk_size:]
    return session
