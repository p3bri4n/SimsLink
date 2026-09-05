"""Mod profiles: named, timestamped, switchable snapshots of "which mods
should be active" — the "save/load library state" feature.

profile_mods only stores membership (which mod_ids belong to a profile) — it
never independently tracks activation state. Activating ("loading") a
profile re-enables exactly the mods it captured, using the same
mod_manager.enable() symlink toggling as everywhere else in the app — no new
activation mechanism is invented here. Deliberately additive-only: it never
disables a mod that isn't part of the snapshot (see activate_profile()'s own
docstring for the real-world case that made the earlier exact-set-enforcing
version a problem).

Membership is replaced wholesale via set_profile_mods() rather than
incremental add/remove calls — simpler for a UI that captures "the mods
active right now" as a snapshot than one that manages per-mod membership.

created_date records when the snapshot was taken ("saved"), same
datetime.now(timezone.utc).isoformat() convention used for mods.install_date
elsewhere — nullable because it was added in a later migration and profiles
created before that have no timestamp to backfill.

record_missing_mod_if_saved()/list_missing_mods()/dismiss_missing_mod() cover
a related but separate concern: a mod that gets deleted while it's still
part of a saved state. profile_mods.mod_id has ON DELETE CASCADE, so the
instant a mod is deleted, every profile's membership record of it vanishes
immediately, with no trace — there's no way to detect this after the fact,
and no backstop at profile-load time either (activate_profile() can never
see a stale member for the same reason: cascade already removed it by the
time any profile is next loaded). The only place this can be caught is
*before* the deletion happens, while the mod's name/link are still
readable — see record_missing_mod_if_saved()'s docstring for exactly where
that hook has to sit and why.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import mod_manager
from .config import Config


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    id: int
    name: str
    mod_ids: list[str]
    created_date: str | None = None


def create_profile(name: str, conn: sqlite3.Connection) -> int:
    name = name.strip()
    if not name:
        raise ProfileError("Profile name cannot be empty")
    try:
        cursor = conn.execute(
            "INSERT INTO profiles (name, created_date) VALUES (?, ?)",
            (name, datetime.now(timezone.utc).isoformat()),
        )
    except sqlite3.IntegrityError as exc:
        raise ProfileError(f"A profile named '{name}' already exists") from exc
    conn.commit()
    return cursor.lastrowid


def delete_profile(profile_id: int, conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    conn.commit()


def _mod_ids_for(profile_id: int, conn: sqlite3.Connection) -> list[str]:
    return [
        row["mod_id"]
        for row in conn.execute(
            "SELECT mod_id FROM profile_mods WHERE profile_id = ? ORDER BY mod_id", (profile_id,)
        )
    ]


def get_profile(profile_id: int, conn: sqlite3.Connection) -> Profile:
    row = conn.execute("SELECT id, name, created_date FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        raise ProfileError(f"No such profile: {profile_id}")
    return Profile(id=row["id"], name=row["name"], mod_ids=_mod_ids_for(profile_id, conn), created_date=row["created_date"])


def list_profiles(conn: sqlite3.Connection) -> list[Profile]:
    # Most recently saved first — a timestamped list of "saved states" reads
    # more naturally that way than alphabetically; a profile predating the
    # created_date column (NULL) sorts last rather than crashing the order.
    rows = conn.execute("SELECT id, name, created_date FROM profiles ORDER BY created_date DESC").fetchall()
    return [
        Profile(id=row["id"], name=row["name"], mod_ids=_mod_ids_for(row["id"], conn), created_date=row["created_date"])
        for row in rows
    ]


def set_profile_mods(profile_id: int, mod_ids: list[str], conn: sqlite3.Connection) -> None:
    get_profile(profile_id, conn)  # raises ProfileError if unknown
    conn.execute("DELETE FROM profile_mods WHERE profile_id = ?", (profile_id,))
    for mod_id in mod_ids:
        conn.execute("INSERT INTO profile_mods (profile_id, mod_id) VALUES (?, ?)", (profile_id, mod_id))
    conn.commit()


def activate_profile(profile_id: int, *, config: Config, conn: sqlite3.Connection) -> None:
    """Re-enables exactly the mods captured in this saved state — anything
    disabled since the save was taken is turned back on. Deliberately
    additive-only: earlier versions also disabled every mod *not* in the
    snapshot, which meant loading an older save would silently disable a
    mod installed since (e.g. extracting an archive through SimsLink's own
    broken-folder repair, then loading a save from before that — the newly
    installed mod would vanish from the active set with no indication why).
    A "restore point" shouldn't have that side effect: it should bring back
    what was saved, not punish anything added since. A mod_id the snapshot
    references that no longer exists (deleted since) is silently skipped
    rather than raising — same reasoning as elsewhere in this module.

    Fails fast (propagates mod_manager/dependencies errors) on the first
    mod that can't be enabled — e.g. an unresolved required dependency —
    rather than silently partially applying the profile.
    """
    profile = get_profile(profile_id, conn)
    for mod_id in profile.mod_ids:
        row = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()
        if row is not None and not row["active"]:
            mod_manager.enable(mod_id, config=config, conn=conn)


@dataclass(frozen=True)
class MissingMod:
    id: int
    mod_id: str
    name: str
    curseforge_url: str | None
    source_profile_names: str
    detected_date: str


def record_missing_mod_if_saved(mod_id: str, conn: sqlite3.Connection) -> None:
    """Call this immediately *before* deleting a mod (see main.py's
    DELETE /api/mods/{mod_id} route) — never from mod_manager.delete()
    itself, which is also used internally by
    download_watcher.confirm_replace()/broken_mods.fix_rezipped_mod() as the
    first half of a delete-then-reinstall replace step. That isn't a real
    removal (the mod comes right back, usually under the same id), so it
    must never trigger a "you might want to reinstall this" reminder —
    only an explicit, user-initiated deletion should.

    Ordering matters: profile_mods.mod_id has ON DELETE CASCADE, so once the
    mods row is actually gone, every profile_mods row referencing it is
    gone too, and so is any chance of reading the mod's name/CurseForge
    link. This has to run while the mod still exists.

    No-ops when the mod isn't part of any saved state — deleting a mod
    nobody ever saved shouldn't produce a reminder. Idempotent per mod_id
    (INSERT OR IGNORE against missing_mods.mod_id's UNIQUE constraint): if
    this mod ever gets flagged again (e.g. deleted a second time after
    being reinstalled), it doesn't create a duplicate entry.
    """
    profile_rows = conn.execute(
        "SELECT DISTINCT profile_id FROM profile_mods WHERE mod_id = ?", (mod_id,)
    ).fetchall()
    if not profile_rows:
        return

    mod_row = conn.execute("SELECT name, links FROM mods WHERE id = ?", (mod_id,)).fetchone()
    if mod_row is None:
        return

    curseforge_url = None
    if mod_row["links"]:
        curseforge_url = json.loads(mod_row["links"]).get("curseforge_url")

    profile_names = sorted(
        {
            conn.execute(
                "SELECT name FROM profiles WHERE id = ?", (row["profile_id"],)
            ).fetchone()["name"]
            for row in profile_rows
        }
    )

    conn.execute(
        "INSERT OR IGNORE INTO missing_mods "
        "(mod_id, name, curseforge_url, source_profile_names, detected_date) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            mod_id,
            mod_row["name"],
            curseforge_url,
            ", ".join(profile_names),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def list_missing_mods(conn: sqlite3.Connection) -> list[MissingMod]:
    """Live-filtered rather than actively purged: a mod_id that's since
    reappeared (typically reinstalled under the same freed slug) is treated
    as resolved and silently excluded here, but its row is left in place —
    if it goes missing again later, the original reminder just becomes
    visible again instead of a fresh duplicate being inserted (blocked by
    missing_mods.mod_id's UNIQUE constraint anyway)."""
    rows = conn.execute("SELECT * FROM missing_mods ORDER BY detected_date DESC").fetchall()
    return [
        MissingMod(
            id=row["id"],
            mod_id=row["mod_id"],
            name=row["name"],
            curseforge_url=row["curseforge_url"],
            source_profile_names=row["source_profile_names"],
            detected_date=row["detected_date"],
        )
        for row in rows
        if conn.execute("SELECT 1 FROM mods WHERE id = ?", (row["mod_id"],)).fetchone() is None
    ]


def dismiss_missing_mod(entry_id: int, conn: sqlite3.Connection) -> None:
    """Manual "I've seen this" dismissal — purely removes the reminder
    itself, no other side effect (nothing to reverse; the mod stays gone
    either way)."""
    conn.execute("DELETE FROM missing_mods WHERE id = ?", (entry_id,))
    conn.commit()
