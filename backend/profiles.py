"""Mod profiles: named, switchable sets of "which mods should be active."

profile_mods only stores membership (which mod_ids belong to a profile) — it
never independently tracks activation state. Activating a profile means
"make exactly this set of mods active, and nothing else," using the same
mod_manager.enable()/disable() symlink toggling as everywhere else in the
app; no new activation mechanism is invented here.

Membership is replaced wholesale via set_profile_mods() rather than
incremental add/remove calls — simpler for a UI that captures "the mods
active right now" as a snapshot than one that manages per-mod membership.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import mod_manager
from .config import Config


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    id: int
    name: str
    mod_ids: list[str]


def create_profile(name: str, conn: sqlite3.Connection) -> int:
    name = name.strip()
    if not name:
        raise ProfileError("Profile name cannot be empty")
    try:
        cursor = conn.execute("INSERT INTO profiles (name) VALUES (?)", (name,))
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
    row = conn.execute("SELECT id, name FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        raise ProfileError(f"No such profile: {profile_id}")
    return Profile(id=row["id"], name=row["name"], mod_ids=_mod_ids_for(profile_id, conn))


def list_profiles(conn: sqlite3.Connection) -> list[Profile]:
    rows = conn.execute("SELECT id, name FROM profiles ORDER BY name COLLATE NOCASE").fetchall()
    return [Profile(id=row["id"], name=row["name"], mod_ids=_mod_ids_for(row["id"], conn)) for row in rows]


def set_profile_mods(profile_id: int, mod_ids: list[str], conn: sqlite3.Connection) -> None:
    get_profile(profile_id, conn)  # raises ProfileError if unknown
    conn.execute("DELETE FROM profile_mods WHERE profile_id = ?", (profile_id,))
    for mod_id in mod_ids:
        conn.execute("INSERT INTO profile_mods (profile_id, mod_id) VALUES (?, ?)", (profile_id, mod_id))
    conn.commit()


def activate_profile(profile_id: int, *, config: Config, conn: sqlite3.Connection) -> None:
    """Makes exactly this profile's mods active, deactivating every other
    installed mod. Fails fast (propagates mod_manager/dependencies errors)
    on the first mod that can't be enabled — e.g. an unresolved required
    dependency — rather than silently partially applying the profile."""
    profile = get_profile(profile_id, conn)
    target = set(profile.mod_ids)
    for row in conn.execute("SELECT id, active FROM mods").fetchall():
        should_be_active = row["id"] in target
        is_active = bool(row["active"])
        if should_be_active and not is_active:
            mod_manager.enable(row["id"], config=config, conn=conn)
        elif not should_be_active and is_active:
            mod_manager.disable(row["id"], config=config, conn=conn)
