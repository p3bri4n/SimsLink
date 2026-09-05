"""Bulk-disables active mods flagged incompatible with the current game
version, cascading to whatever active mods locally declare a *required*
dependency on one of them, and re-enables them later once a CurseForge
resync (curseforge_dependencies.py's "Synchroniser") shows they're no
longer incompatible.

Diagnostic/remediation tool for the "crashes after a game patch, dozens of
mods are stale" scenario — same "suspicion is not confirmation" spirit as
conflict_detector.py/broken_mods.py: preview_quarantine() only ever computes
and returns a candidate list, it never disables anything on its own. The
frontend must always show that preview and get explicit confirmation before
calling quarantine_mods().

The dependency cascade is best-effort, not exhaustive: it can only see what
`dependencies` already has rows for, which itself is only ever populated
on-demand or via the bulk CurseForge sync (see curseforge_dependencies.py) —
a dependent mod that was never synced/detected won't be found. It's still
useful as a first pass, not a guarantee.

compat_quarantine (the table) records exactly which mods *this feature*
disabled, not a full snapshot of "what was active before" (unlike
profiles.py's save/load) — the point is to be able to bring back precisely
what this action turned off, one mod at a time, as each one's own
compat_status clears up.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import dependencies
from . import mod_manager
from .config import Config


@dataclass(frozen=True)
class QuarantineCandidate:
    mod_id: str
    name: str
    # 'incompatible' for a seed mod, or the mod_id of the dependency that
    # pulled this one in via the required-dependency cascade.
    reason: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _active_incompatible_mods(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name FROM mods WHERE active = 1 AND compat_status = 'incompatible'"
    ).fetchall()


def _active_required_dependents(conn: sqlite3.Connection, mod_id: str) -> list[sqlite3.Row]:
    """Active mods with a 'required' dependency on `mod_id`, matched by
    either the local-id link or the curseforge_id link (whichever the
    dependency row happens to use — see dependencies.py's schema note on
    depends_on_mod_id vs depends_on_curseforge_id)."""
    row = conn.execute("SELECT curseforge_id FROM mods WHERE id = ?", (mod_id,)).fetchone()
    curseforge_id = row["curseforge_id"] if row is not None else None
    return conn.execute(
        "SELECT DISTINCT m.id, m.name FROM dependencies d "
        "JOIN mods m ON m.id = d.mod_id "
        "WHERE d.dependency_type = 'required' AND m.active = 1 AND ("
        "  d.depends_on_mod_id = ? "
        "  OR (? IS NOT NULL AND d.depends_on_curseforge_id = ?)"
        ")",
        (mod_id, curseforge_id, curseforge_id),
    ).fetchall()


def preview_quarantine(conn: sqlite3.Connection) -> list[QuarantineCandidate]:
    """Every currently-active incompatible mod, plus (iteratively, to a
    fixed point) every currently-active mod that locally required-depends on
    one already in the set — so disabling a mod's dependency doesn't leave
    its dependent mod active and broken. Never writes anything; purely a
    preview for the caller to confirm."""
    candidates: dict[str, QuarantineCandidate] = {
        row["id"]: QuarantineCandidate(mod_id=row["id"], name=row["name"], reason="incompatible")
        for row in _active_incompatible_mods(conn)
    }
    changed = True
    while changed:
        changed = False
        for mod_id in list(candidates):
            for dep_row in _active_required_dependents(conn, mod_id):
                if dep_row["id"] not in candidates:
                    candidates[dep_row["id"]] = QuarantineCandidate(
                        mod_id=dep_row["id"], name=dep_row["name"], reason=mod_id
                    )
                    changed = True
    return list(candidates.values())


def quarantine_mods(
    candidates: list[QuarantineCandidate], *, config: Config, conn: sqlite3.Connection
) -> list[str]:
    """Disables and records every still-active candidate. A candidate that's
    already inactive (e.g. the preview is stale, or the user disabled it by
    hand in the meantime) is silently skipped rather than erroring — nothing
    left to do for it. Re-quarantining a mod that's already tracked just
    refreshes its reason/date. Returns the mod_ids actually quarantined."""
    quarantined: list[str] = []
    for candidate in candidates:
        row = conn.execute("SELECT active FROM mods WHERE id = ?", (candidate.mod_id,)).fetchone()
        if row is None or not row["active"]:
            continue
        mod_manager.disable(candidate.mod_id, config=config, conn=conn)
        conn.execute(
            "INSERT INTO compat_quarantine (mod_id, reason, quarantined_date) VALUES (?, ?, ?) "
            "ON CONFLICT(mod_id) DO UPDATE SET reason = excluded.reason, "
            "quarantined_date = excluded.quarantined_date",
            (candidate.mod_id, candidate.reason, _now_iso()),
        )
        conn.commit()
        quarantined.append(candidate.mod_id)
    return quarantined


def list_quarantined(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT q.mod_id, q.reason, q.quarantined_date, m.name, m.compat_status "
        "FROM compat_quarantine q JOIN mods m ON m.id = q.mod_id "
        "ORDER BY q.quarantined_date DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def forget_quarantined(mod_id: str, conn: sqlite3.Connection) -> None:
    """Manual dismiss: stops tracking a mod for release_ready_mods() without
    touching its active/enabled state either way."""
    conn.execute("DELETE FROM compat_quarantine WHERE mod_id = ?", (mod_id,))
    conn.commit()


def release_ready_mods(*, config: Config, conn: sqlite3.Connection) -> dict:
    """Re-enables every quarantined mod whose compat_status is no longer
    'incompatible' (a CurseForge resync after the author shipped an update —
    see curseforge_dependencies.py's "Synchroniser" — is what actually
    refreshes that field) and drops it from the quarantine list. A mod still
    blocked on an unresolved *confirmed* required dependency (enable()'s own
    check, via dependencies.check_required()) stays quarantined and is
    reported under 'failed' rather than silently dropped."""
    rows = conn.execute(
        "SELECT q.mod_id, m.compat_status FROM compat_quarantine q JOIN mods m ON m.id = q.mod_id"
    ).fetchall()
    released: list[str] = []
    still_incompatible: list[str] = []
    failed: list[dict] = []
    for row in rows:
        if row["compat_status"] == "incompatible":
            still_incompatible.append(row["mod_id"])
            continue
        try:
            mod_manager.enable(row["mod_id"], config=config, conn=conn)
        except (mod_manager.ModManagerError, dependencies.UnresolvedRequiredDependencyError) as exc:
            failed.append({"mod_id": row["mod_id"], "error": str(exc)})
            continue
        conn.execute("DELETE FROM compat_quarantine WHERE mod_id = ?", (row["mod_id"],))
        conn.commit()
        released.append(row["mod_id"])
    return {"released": released, "still_incompatible": still_incompatible, "failed": failed}
