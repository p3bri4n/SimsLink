"""Links an already-installed mod to its real CurseForge entry via file-
fingerprint matching, for mods that got no such link at install time —
overwhelmingly the loose-imported population (loose_mods.py: one bare file
at Mods/ root adopted as one mod, no metadata attached). Scoped to that
population specifically (`is_loose_import = 1`) rather than every mod with
no `curseforge_id`, since a loose import is reliably single-file — trying
to pick "the" representative file for an arbitrary multi-file mod folder
would be a real ambiguity this module has no basis to resolve.

Chunked and resumable (start_session()/run_step()) rather than one big
blocking call — hashing a real library's worth of files is slow enough
(curseforge_fingerprint()'s mixing loop is still Python-level per word) that
a user watching a progress popup needs live updates, and a way to stop
without losing what's already been matched. run_step() commits its DB
writes immediately, so "stop" is purely a frontend decision (just stop
calling .../step) — nothing on the backend needs to know a run was cut
short. On-demand only, triggered from a header button — never automatic —
and Direct Mode only (requires a working CurseForgeClient).

Only an *exact* fingerprint match ever gets applied — CurseForge's own
partial-match results are ignored by CurseForgeClient.match_fingerprints()
already, so nothing here even sees them. Confirmed directly against the
real API: every exact match checked identified the correct mod *and* the
correct file variant (e.g. the right fan translation) — see CLAUDE.md.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import curseforge

# A handful of already-merged .package files in a real library can run into
# the hundreds of MB — curseforge_fingerprint()'s mixing loop is still
# Python-level per 4-byte word (struct.unpack_from only sped up the byte
# extraction), so those dominate a run's whole runtime for no real benefit:
# an already-merged file isn't a realistic "shattered loose CC" candidate
# in the first place. Skipped rather than counted as checked-but-unmatched.
_SIZE_CAP_BYTES = 25 * 1024 * 1024

# Mods hashed + matched per run_step() call. Small enough that a progress
# popup updates every few seconds rather than in one multi-minute jump;
# large enough that most of a chunk's time is still real work, not request
# overhead. Hashing dominates the per-chunk cost, not the two API calls.
CHUNK_SIZE = 20


@dataclass
class MatchSession:
    """One in-progress bulk-match run — held in app.state (main.py) between
    POST .../start and repeated POST .../step calls. `remaining` shrinks
    each step(); `done` is true once nothing's left."""

    remaining: list[tuple[str, str]] = field(default_factory=list)  # (mod_id, library_path)
    total: int = 0
    checked: int = 0
    matched: int = 0
    skipped_too_large: int = 0

    @property
    def done(self) -> bool:
        return not self.remaining


def _candidate_mods(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT id, library_path FROM mods WHERE is_loose_import = 1 AND curseforge_id IS NULL"
    ).fetchall()
    return [(row["id"], row["library_path"]) for row in rows]


def _representative_file(library_path: str) -> Path | None:
    for path in sorted(Path(library_path).rglob("*")):
        if path.is_file() and path.suffix.lower() in (".package", ".ts4script"):
            return path
    return None


def start_session(conn: sqlite3.Connection) -> MatchSession:
    """Always builds a fresh candidate list from the DB — a mod matched by
    an earlier (possibly stopped-early) run already has curseforge_id set
    by now, so it's naturally excluded here rather than needing its own
    "already done" bookkeeping."""
    candidates = _candidate_mods(conn)
    return MatchSession(remaining=candidates, total=len(candidates))


def run_step(
    session: MatchSession,
    conn: sqlite3.Connection,
    client: curseforge.CurseForgeClient,
    chunk_size: int = CHUNK_SIZE,
) -> MatchSession:
    """Raises curseforge.CurseForgeError/requests.RequestException on a
    transient CurseForge/network failure — deliberately *not* caught here.
    `session` is left completely untouched until the API calls below
    succeed (chunk is only sliced off `session.remaining`, and `checked`
    only advanced, at the very end), specifically so that a caller
    re-running run_step() with the same `session` after a failure retries
    the *exact same* chunk — never skips it, never double-counts it. See
    main.py's step route, which is what actually catches this and lets the
    frontend retry with backoff instead of the whole run just dying.
    """
    chunk = session.remaining[:chunk_size]

    fingerprint_by_mod_id: dict[str, int] = {}
    newly_skipped_too_large = 0
    for mod_id, library_path in chunk:
        try:
            path = _representative_file(library_path)
            if path is None:
                continue
            if path.stat().st_size > _SIZE_CAP_BYTES:
                newly_skipped_too_large += 1
                continue
            fingerprint_by_mod_id[mod_id] = curseforge.curseforge_fingerprint(path.read_bytes())
        except OSError:
            # Missing/unreadable file (deleted mid-run, permission hiccup,
            # a broken symlink, ...) — skip just this one mod rather than
            # failing the whole chunk over something CurseForge has nothing
            # to do with.
            continue

    fingerprints = list(fingerprint_by_mod_id.values())
    curseforge_id_by_fingerprint = client.match_fingerprints(fingerprints) if fingerprints else {}

    matched_curseforge_ids = sorted(set(curseforge_id_by_fingerprint.values()))
    curseforge_mods_by_id = (
        {mod.mod_id: mod for mod in client.get_mods(matched_curseforge_ids)} if matched_curseforge_ids else {}
    )

    for mod_id, fingerprint in fingerprint_by_mod_id.items():
        curseforge_id = curseforge_id_by_fingerprint.get(fingerprint)
        if curseforge_id is None:
            continue
        curseforge_mod = curseforge_mods_by_id.get(curseforge_id)
        if curseforge_mod is None:
            continue
        _apply_match(conn, mod_id, curseforge_mod)
        session.matched += 1

    # Only advanced once every risky (network) operation above has already
    # succeeded — see the docstring.
    session.skipped_too_large += newly_skipped_too_large
    session.checked += len(chunk)
    session.remaining = session.remaining[chunk_size:]
    return session


def _apply_match(conn: sqlite3.Connection, mod_id: str, curseforge_mod: curseforge.CurseForgeMod) -> None:
    # COALESCE: only fills in fields that are still empty — never overwrites
    # something already set by another path (e.g. a real author a Direct-
    # Mode catalog install already attached). short_description/category/
    # name come free with the same bulk get_mods() call that found the
    # match — full_description deliberately isn't fetched here:
    # CurseForgeClient only exposes it via get_description(mod_id), a
    # *separate* request per mod, which would mean one more API call per
    # newly-matched mod on top of match_fingerprints()/get_mods() already
    # required for every chunk.
    #
    # curseforge_name is deliberately a separate column from `name` (see
    # migration 10), never overwriting it — a loose import's `name` is
    # whatever local text it was adopted under (e.g. a raw filename), while
    # curseforge_name is CurseForge's own real name for the same mod; the
    # frontend prefers curseforge_name for display whenever it's set,
    # bypassing the "Simplified names" toggle entirely since it's already
    # the real name, not something to guess-clean from messy local text.
    links = json.dumps({"curseforge_url": curseforge_mod.curseforge_url}) if curseforge_mod.curseforge_url else None
    conn.execute(
        "UPDATE mods SET curseforge_id = ?, author = COALESCE(author, ?), category = COALESCE(category, ?), "
        "short_description = COALESCE(short_description, ?), thumbnail_url = COALESCE(thumbnail_url, ?), "
        "links = COALESCE(links, ?), curseforge_name = COALESCE(curseforge_name, ?) WHERE id = ?",
        (
            curseforge_mod.mod_id,
            curseforge_mod.author,
            curseforge_mod.category,
            curseforge_mod.short_description,
            curseforge_mod.thumbnail_url,
            links,
            curseforge_mod.name,
            mod_id,
        ),
    )
    conn.commit()
