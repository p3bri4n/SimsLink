"""FastAPI backend — SimsLink's application layer (see CLAUDE.md's
"Architecture"). All five views (Library, Catalog, Updates, Crash Mode,
Settings) are wired up here.

Serves `frontend/` as static files too, so the whole app is reachable at
http://localhost:8000/ with a single origin — no CORS needed. Everything
under /api/ is JSON; every other path falls through to the static mount.

This module only translates HTTP <-> the business-logic modules in this same
package (mod_manager.py/dependencies.py/db.py/config.py/...); it stays a
thin routing layer and shouldn't grow feature logic of its own.

`create_app()` takes a `Config` instead of resolving one from the
environment at import time, specifically so tests can build an app against a
throwaway temp Config/DB via FastAPI's TestClient.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sqlite3
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import blacklist as blacklist_module
from . import broken_mods
from . import cache_cleaner
from . import compat_quarantine
from . import conflict_detector
from . import crash_analyzer
from . import curseforge
from . import curseforge_dependencies
from . import curseforge_match
from . import db
from . import dependencies
from . import download_watcher
from . import game_options
from . import loose_mods
from . import mod_manager
from . import path_settings
from . import profiles as profiles_module
from . import scanner
from .config import Config

APP_VERSION = "0.1.0"  # keep in sync with pyproject.toml's [project].version
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Module-level (not inside create_app()) so FastAPI/pydantic can resolve the
# "CatalogSort"/"CatalogPeriod" forward references it builds from this
# module's postponed (`from __future__ import annotations`) type hints —
# a route-local definition isn't visible in the module namespace pydantic
# looks the name up in, which raised a PydanticUserError ("is not fully
# defined") at request time instead of at import time.
CatalogSort = Literal["popularity", "downloads", "updated", "newest", "name"]
CatalogPeriod = Literal["week", "month", "quarter", "year"]

logger = logging.getLogger(__name__)


class OpenExternalRequest(BaseModel):
    url: str


class BisectionReport(BaseModel):
    crash_occurred: bool


class ConfirmFaultyRequest(BaseModel):
    mod_id: str


class ReplaceDownloadRequest(BaseModel):
    mod_id: str


class SuggestTranslationRequest(BaseModel):
    source_mod_id: str


class CreateProfileRequest(BaseModel):
    name: str


class SetProfileModsRequest(BaseModel):
    mod_ids: list[str]


class AddBlacklistEntryRequest(BaseModel):
    pattern: str
    note: str | None = None


class ExtractZipsRequest(BaseModel):
    zip_paths: list[str]


class UpdatePathsRequest(BaseModel):
    sims4_game_dir: str | None = None
    sims4_user_dir: str | None = None
    library_dir: str | None = None


class PickFolderRequest(BaseModel):
    initial_dir: str | None = None


class MergeLooseModsRequest(BaseModel):
    mod_ids: list[str]
    new_name: str


class SetNamespaceOverrideRequest(BaseModel):
    value: str | None = None


class QuarantineModsRequest(BaseModel):
    mod_ids: list[str]


class PendingDownloadStore:
    """In-memory queue of detected-but-unconfirmed downloads (Assisted
    Mode). Written to by DownloadWatcher's background thread, read/drained
    by FastAPI request threads — a plain dict would race, so every access
    goes through the lock. Not persisted: if the app restarts mid-decision,
    the file is still sitting in the download folder and gets re-detected
    on the next watcher event (or the next start_watcher() call), so
    nothing is lost."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def add(self, path: Path, candidate_mod_id: str | None, candidate_mod_name: str | None) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._items[token] = {
                "token": token,
                "path": path,
                "filename": path.name,
                "candidate_mod_id": candidate_mod_id,
                "candidate_mod_name": candidate_mod_name,
            }
        return token

    def list(self) -> list[dict]:
        # Never leak the raw filesystem path to the client — filename is all
        # the frontend needs to render the dialog.
        with self._lock:
            return [{k: v for k, v in item.items() if k != "path"} for item in self._items.values()]

    def pop(self, token: str) -> dict | None:
        with self._lock:
            return self._items.pop(token, None)


def create_app(config: Config, *, db_path: Path | None = None) -> FastAPI:
    """`db_path` defaults to `config.db_path` (the real, fixed XDG data
    location — see config.py) for production use. Tests override it to an
    isolated tmp_path database instead of touching that shared real file,
    the same way tests/conftest.py's `conn` fixture already does."""
    db_path = db_path or config.db_path
    app = FastAPI(title="SimsLink")
    db.init_db(db_path)

    # Layer any persisted path overrides (Settings > paths — see
    # path_settings.py) on top of the .env-derived config, before anything
    # below captures `config` in a closure (routes, the mods/download
    # watchers). A fresh DB with no settings rows yet — every test, or a
    # real first launch — leaves config unchanged.
    _startup_conn = db.connect(db_path)
    try:
        config = path_settings.apply_stored_overrides(config, _startup_conn)
    finally:
        _startup_conn.close()

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        # DEBUG-level tracing of every request — distinct from the targeted
        # INFO-level logging on individual mutating routes below, which
        # covers user-meaningful actions (installed/enabled/deleted a mod,
        # ...) rather than raw HTTP traffic. Configured via LOG_LEVEL; see
        # logging_config.py.
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        logger.debug("%s %s -> %d (%.1fms)", request.method, request.url.path, response.status_code, duration_ms)
        return response

    # Resolved once at app creation, not per-request — verify_key() is a
    # network call.
    direct_mode = False
    cf_client: curseforge.CurseForgeClient | None = None
    # Single in-progress CurseForge fingerprint-match run, if any — see the
    # /api/settings/match-curseforge/start and .../step routes below. A
    # local single-user app only ever has one such run at a time.
    match_session: curseforge_match.MatchSession | None = None
    # Same idea, for the bulk "Synchroniser" sync-curseforge run (every
    # already-linked mod's dependencies/compat_status) — see the
    # /api/settings/sync-curseforge/start and .../step routes below.
    sync_session: curseforge_dependencies.SyncSession | None = None
    if config.has_api_key:
        candidate = curseforge.CurseForgeClient(config.curseforge_api_key)
        if candidate.verify_key():
            direct_mode = True
            cf_client = candidate
        else:
            logger.warning("CURSEFORGE_API_KEY is set but was rejected — falling back to Assisted Mode")
    logger.info("Starting in %s Mode", "Direct" if direct_mode else "Assisted")

    def require_client() -> curseforge.CurseForgeClient:
        if cf_client is None:
            raise HTTPException(
                status_code=400, detail="Catalog requires Direct Mode (a valid CURSEFORGE_API_KEY)"
            )
        return cf_client

    def get_conn():
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    # Assisted Mode download detection. DownloadWatcher itself is built here
    # (so it shares report_download's wiring) but never started here —
    # starting a real watchdog Observer thread as a side effect of building
    # the app would fire on every test that calls create_app(). desktop.py
    # starts/stops app.state.download_watcher around the pywebview window's
    # lifecycle instead; tests call app.state.report_download(path) directly
    # to simulate a detection with no real filesystem watcher involved.
    pending_downloads = PendingDownloadStore()

    def report_download(path: Path) -> None:
        # Runs on DownloadWatcher's own background thread in production —
        # never touches the request-scoped `conn` dependency; opens its own
        # short-lived connection instead (sqlite3 connections aren't safe to
        # share across threads).
        conn = db.connect(db_path)
        try:
            candidate_mod_id, candidate_mod_name = download_watcher.match_existing_mod(path, conn)
        finally:
            conn.close()
        pending_downloads.add(path, candidate_mod_id, candidate_mod_name)

    app.state.download_watcher = download_watcher.DownloadWatcher(config, report_download)
    app.state.report_download = report_download

    # Mods/ real-time watcher + startup catch-up scan (CLAUDE.md's "Startup
    # scan"). Same not-started-here rule as the download watcher above, for
    # the same reason: building the app must stay side-effect-free.
    #
    # ModsFolderWatcher's on_change fires on every single filesystem event
    # under Mods/ with no debounce of its own (see scanner.py) — a symlink
    # toggle alone touches several paths — so this debounces before paying
    # for a rescan, and (like report_download) opens its own connection
    # since it runs off the request-scoped `conn` dependency's thread.
    _scan_debounce_lock = threading.Lock()
    _scan_timer: threading.Timer | None = None
    _SCAN_DEBOUNCE_SECONDS = 2.0

    def run_mods_rescan() -> None:
        # Adopts anything dropped directly under Mods/ that isn't managed
        # yet, then catches up on size/mtime changes for everything already
        # tracked. Shared by the startup catch-up scan and the real-time
        # watcher's debounced rescan below — until this was unified, only
        # the startup path called import_untracked_mods(), so a new real
        # folder dropped in while the app was already running (e.g. a
        # duplicated mod folder) stayed invisible until the next full
        # restart even though the watcher fired for it.
        conn = db.connect(db_path)
        try:
            scanner.import_untracked_mods(config, conn)
            scanner.incremental_scan(config, conn)
        finally:
            conn.close()

    def schedule_mods_rescan() -> None:
        nonlocal _scan_timer
        with _scan_debounce_lock:
            if _scan_timer is not None:
                _scan_timer.cancel()
            _scan_timer = threading.Timer(_SCAN_DEBOUNCE_SECONDS, run_mods_rescan)
            _scan_timer.daemon = True
            _scan_timer.start()

    app.state.mods_watcher = scanner.ModsFolderWatcher(config, schedule_mods_rescan)
    app.state.run_startup_scan = run_mods_rescan
    app.state.schedule_mods_rescan = schedule_mods_rescan  # exposed for tests, same idea as report_download

    def mod_link_state(row: sqlite3.Row) -> str:
        """'unlinked' | 'linked' | 'incompatible' | 'update_available'.

        A single precomputed field (rather than making the frontend combine
        curseforge_id/compat_status/latest_version itself) — mirrors how
        compat_status is already a precomputed enum, not raw version ranges.

        'update_available' requires the *latest known* file (as of the last
        explicit POST /api/updates/check — never a live network call here)
        to itself be compatible with the current game version, not just
        newer; 'incompatible' requires no such update pending. A mod with an
        update pending that ISN'T yet compatible matches neither — it stays
        'linked', same as before an update existed, rather than guessing
        which of the two it's closer to.
        """
        if not row["curseforge_id"]:
            return "unlinked"
        update_available = bool(row["latest_version"]) and row["latest_version"] != row["installed_version"]
        if update_available:
            update_compat = curseforge.compat_status(
                row["latest_version_min"], row["latest_version_max"], config.game_version
            )
            if update_compat == "compatible":
                return "update_available"
        elif row["compat_status"] == "incompatible":
            return "incompatible"
        return "linked"

    def mod_summary(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "author": row["author"],
            "category": row["category"],
            "primary_type": row["primary_type"],
            "installed_version": row["installed_version"],
            "compat_status": row["compat_status"],
            "link_state": mod_link_state(row),
            "active": bool(row["active"]),
            "short_description": row["short_description"],
            "thumbnail_url": row["thumbnail_url"],
            "is_loose_import": bool(row["is_loose_import"]),
            "namespace_override": row["namespace_override"],
            "curseforge_id": row["curseforge_id"],
            "curseforge_name": row["curseforge_name"],
        }

    def get_mod_row(mod_id: str, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM mods WHERE id = ?", (mod_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No such mod: {mod_id}")
        return row

    def dependency_dict(link: dependencies.DependencyLink, conn: sqlite3.Connection) -> dict:
        # Never resolve/return a pre-translated label here — the frontend's
        # i18n layer owns presentation text (CLAUDE.md: no UI string outside
        # frontend/i18n/). This only returns the data needed to build one.
        resolved_name = None
        if link.depends_on_mod_id is not None:
            target = conn.execute(
                "SELECT name FROM mods WHERE id = ?", (link.depends_on_mod_id,)
            ).fetchone()
            resolved_name = target["name"] if target is not None else None
        elif link.depends_on_curseforge_id is not None:
            target = conn.execute(
                "SELECT name FROM mods WHERE curseforge_id = ?", (link.depends_on_curseforge_id,)
            ).fetchone()
            resolved_name = target["name"] if target is not None else None
        return {
            "id": link.id,
            "dependency_type": link.dependency_type,
            "confidence": link.confidence,
            "mandatory": link.mandatory,
            "resolved_name": resolved_name,
            "depends_on_curseforge_id": link.depends_on_curseforge_id,
        }

    @app.get("/api/status")
    def get_status() -> dict:
        # Re-read every call (cheap file read) rather than cached at app
        # creation like direct_mode — the game can rewrite options.ini while
        # SimsLink is running (e.g. the user changes it in-game).
        return {
            "app_version": APP_VERSION,
            "game_version": config.game_version,
            "direct_mode": direct_mode,
            "script_mods_allowed": game_options.script_mods_allowed(config),
        }

    @app.get("/api/mods")
    def list_mods(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        rows = conn.execute("SELECT * FROM mods ORDER BY name COLLATE NOCASE").fetchall()
        return [mod_summary(row) for row in rows]

    @app.get("/api/conflicts")
    def list_conflicts(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        # Purely informational (see conflict_detector.py) — resolves mod_ids
        # to {id, name, author, active, install_date, library_path} here so
        # the frontend never has to look them up itself. install_date/active/
        # author are surfaced so the UI can show the facts relevant to
        # "which one do I keep" without SimsLink guessing an answer itself
        # (CLAUDE.md: suspicion isn't confirmation) — the user decides, we
        # just save them a lookup. `author` specifically drives the
        # frontend's "Duplicate" vs. neutral "Identical content" tag on an
        # 'exact_duplicate_mod' pair: the mod with no known author is
        # assumed the redundant one when the other side has a real author;
        # when both (or neither) have one, neither side gets singled out —
        # see app.js's duplicateTagModIds()/identicalContentTagModIds().
        # `library_path` drives the duplicate comparator's own folder-name
        # display (openDuplicateComparison()) — the on-disk folder name is
        # often the real tell for "which install was a copy" (a trailing
        # "(1)", "- copy", etc.), more so than any of the fields above.
        results = []
        for group in conflict_detector.find_conflicts(conn):
            mods = []
            for mod_id in group.mod_ids:
                row = conn.execute(
                    "SELECT name, author, active, install_date, library_path FROM mods WHERE id = ?", (mod_id,)
                ).fetchone()
                mods.append(
                    {
                        "id": mod_id,
                        "name": row["name"] if row is not None else mod_id,
                        "author": row["author"] if row is not None else None,
                        "active": bool(row["active"]) if row is not None else None,
                        "install_date": row["install_date"] if row is not None else None,
                        "library_path": row["library_path"] if row is not None else None,
                    }
                )
            results.append(
                {"kind": group.kind, "identifier": group.identifier, "mods": mods, "file_count": group.file_count}
            )
        return results

    @app.get("/api/mods/broken")
    def list_broken_mods(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        # Purely informational (see broken_mods.py) — folders under Mods/
        # with nothing the game would actually load, never auto-fixed.
        return [
            {
                "name": folder.name,
                "reason": folder.reason,
                "file_count": folder.file_count,
                "zip_paths": folder.zip_paths,
                "sample_files": folder.sample_files,
            }
            for folder in broken_mods.scan_broken_mods(config, conn)
        ]

    @app.post("/api/mods/broken/{name}/fix")
    def fix_broken_mod(name: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # Only reachable from a confirm-modal-gated frontend action (see
        # broken_mods.py) — never triggered automatically. Only 'empty' and
        # 'unextracted_archive' (single, unambiguous archive) are fixable;
        # anything else raises, translated to 400 below (a multi-archive
        # folder goes through extract-zips instead).
        try:
            mod_id = broken_mods.fix_broken_mod(name, config, conn)
        except broken_mods.BrokenModFixError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"fixed": True, "mod_id": mod_id}

    @app.post("/api/mods/broken/{name}/extract-zips")
    def extract_zips_route(
        name: str, payload: ExtractZipsRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        # Only reachable from a confirm-modal-gated frontend action listing
        # every archive found so the user can pick one or several (see
        # broken_mods.extract_selected_zips()'s docstring for why this is
        # separate from fix_broken_mod()). "deferred" entries are archives
        # that turned out to contain only further archives of their own —
        # extracted into a new Mods/ folder for the next scan to pick up,
        # not installed as a mod.
        try:
            result = broken_mods.extract_selected_zips(name, config, conn, payload.zip_paths)
        except broken_mods.BrokenModFixError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    @app.delete("/api/mods/broken/{name}")
    def delete_broken_folder_route(name: str) -> dict:
        # Manual "just get rid of this" action, gated behind the same
        # confirm-modal pattern as every other destructive action — never
        # triggered automatically. Available for any reason, unlike the
        # reason-specific fix/repair routes above.
        try:
            broken_mods.delete_broken_folder(name, config)
        except broken_mods.BrokenModFixError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"deleted": name}

    @app.post("/api/mods/broken/{name}/open")
    def open_broken_folder(name: str) -> dict:
        # Same "reveal in file manager" idea as open_folder()/
        # open_cache_target() — lets the user actually look at what's
        # inside an 'empty'/'unrecognized' folder (see scan_broken_mods())
        # before deciding what to do with it from the warnings banner.
        entry = config.sims4_mods_dir / name
        if not entry.is_dir():
            raise HTTPException(status_code=404, detail=f"No such unmanaged folder under Mods/: {name}")
        subprocess.Popen(["xdg-open", str(entry)])  # best-effort, Linux-only
        return {"opened": str(entry)}

    @app.post("/api/mods/broken/{name}/attempt-script-repair")
    def attempt_script_repair_route(name: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # Best-effort only, deliberately separate from fix_broken_mod() above
        # — re-zipping an extracted script folder can produce a mod that
        # "installs" but still doesn't load in-game (see broken_mods.py).
        # Only reachable from a confirm-modal-gated frontend action that
        # explicitly warns about this, never triggered automatically.
        try:
            mod_id = broken_mods.attempt_script_repair(name, config, conn)
        except broken_mods.BrokenModFixError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"repaired": True, "mod_id": mod_id}

    @app.get("/api/mods/rezipped")
    def list_rezipped_mods(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        # Purely informational, same spirit as list_broken_mods — an
        # already-tracked mod whose real folder was manually rezipped in
        # place (see broken_mods.py's module docstring).
        return [
            {"mod_id": mod.mod_id, "name": mod.name, "zip_paths": mod.zip_paths}
            for mod in broken_mods.scan_rezipped_mods(config, conn)
        ]

    @app.post("/api/mods/{mod_id}/fix-rezip")
    def fix_rezipped_mod_route(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # Only reachable from a confirm-modal-gated frontend action — never
        # triggered automatically. Only the unambiguous single-archive case
        # is fixable; anything else raises, translated to 400 below.
        try:
            new_mod_id = broken_mods.fix_rezipped_mod(mod_id, config, conn)
        except broken_mods.BrokenModFixError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"fixed": True, "mod_id": new_mod_id}

    @app.get("/api/mods/missing")
    def list_missing_mods_route(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        # Purely informational reminders (see profiles.record_missing_mod_if_saved())
        # — never blocks anything, dismissed manually by the user. Registered
        # here (before the /api/mods/{mod_id} routes above) so "missing"
        # isn't swallowed as a literal mod_id.
        return [
            {
                "id": m.id,
                "mod_id": m.mod_id,
                "name": m.name,
                "curseforge_url": m.curseforge_url,
                "source_profile_names": m.source_profile_names,
                "detected_date": m.detected_date,
            }
            for m in profiles_module.list_missing_mods(conn)
        ]

    @app.delete("/api/mods/missing/{entry_id}")
    def dismiss_missing_mod_route(entry_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        profiles_module.dismiss_missing_mod(entry_id, conn)
        return {"dismissed": entry_id}

    @app.get("/api/mods/loose/suggested-groups")
    def list_loose_grouping_suggestions(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        # Purely informational (see loose_mods.suggest_groupings()) —
        # recomputed fresh on every call, nothing stored, nothing merged
        # until the user explicitly confirms via the route below.
        return [
            {
                "suggested_name": s.suggested_name,
                "mod_ids": s.mod_ids,
                "mod_names": s.mod_names,
                "curseforge_id": s.curseforge_id,
            }
            for s in loose_mods.suggest_groupings(conn)
        ]

    @app.post("/api/mods/loose/merge")
    def merge_loose_mods_route(
        payload: MergeLooseModsRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        # Only reachable from a confirm-modal-gated frontend action — never
        # triggered automatically.
        try:
            new_mod_id = loose_mods.merge_mods(payload.mod_ids, payload.new_name, config=config, conn=conn)
        except (loose_mods.LooseModsError, mod_manager.ModManagerError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info("Merged loose mods %s into %r", payload.mod_ids, new_mod_id)
        return {"merged": True, "mod_id": new_mod_id}

    @app.get("/api/mods/{mod_id}")
    def get_mod(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = get_mod_row(mod_id, conn)
        links = dependencies.list_dependencies(mod_id, conn)
        files = conn.execute(
            "SELECT relative_path FROM mod_files WHERE mod_id = ? ORDER BY relative_path",
            (mod_id,),
        ).fetchall()
        return {
            **mod_summary(row),
            "library_path": row["library_path"],
            "full_description": row["full_description"],
            "install_date": row["install_date"],
            "update_date": row["update_date"],
            "dependencies": [dependency_dict(link, conn) for link in links],
            "files": [f["relative_path"] for f in files],
            # Set whenever curseforge_id is (Direct Mode catalog install, or
            # curseforge_match.py's fingerprint match for a loose import) —
            # mod_summary() deliberately stays list-endpoint-sized and
            # doesn't carry this, so it's added here for the single-mod
            # detail view only (see renderDetail()'s CurseForge link).
            "curseforge_url": mod_curseforge_url(row),
        }

    def get_dependency_row(dependency_id: int, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM dependencies WHERE id = ?", (dependency_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No such dependency: {dependency_id}")
        return row

    @app.post("/api/mods/{mod_id}/detect-curseforge-dependencies")
    def detect_curseforge_dependencies_route(
        mod_id: str, conn: sqlite3.Connection = Depends(get_conn)
    ) -> list[dict]:
        # Direct Mode only, one mod at a time, only for a linked mod (see
        # curseforge_dependencies.py). Unlike detect-translation below, this
        # writes 'suggested' rows directly rather than returning candidates
        # for a separate suggest step — a CurseForge-declared dependency
        # names an exact modId, nothing left to disambiguate once resolved
        # to a local mod. Also refreshes compat_status/game_version_min/max
        # as a byproduct — same get_file() response carries both.
        get_mod_row(mod_id, conn)
        client = require_client()
        try:
            links = curseforge_dependencies.fetch_and_suggest_dependencies(
                mod_id, client=client, conn=conn, game_version=config.game_version
            )
        except curseforge_dependencies.CurseForgeDependenciesError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except curseforge.CurseForgeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return [dependency_dict(link, conn) for link in links]

    @app.post("/api/mods/{mod_id}/detect-translation")
    def detect_translation(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        # On-demand only, one mod at a time — never a full-library scan (see
        # dependencies.py's module docstring and CLAUDE.md's "Translation-mod
        # detection"). Never writes to the DB by itself; suggest-translation
        # below is the only path that does, and only on explicit user action.
        get_mod_row(mod_id, conn)
        signals = dependencies.detect_translation_signals(mod_id, conn)
        results = []
        for signal in signals:
            source_row = conn.execute(
                "SELECT name FROM mods WHERE id = ?", (signal.source_mod_id,)
            ).fetchone()
            results.append(
                {
                    "source_mod_id": signal.source_mod_id,
                    "source_mod_name": source_row["name"] if source_row is not None else signal.source_mod_id,
                    "method": signal.method,
                    "strength": signal.strength,
                }
            )
        return results

    @app.post("/api/mods/{mod_id}/suggest-translation")
    def suggest_translation_route(
        mod_id: str, payload: SuggestTranslationRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        # Always lands as confidence='suggested' — dependencies.py enforces
        # this itself, but the point stands: this route can propose a link,
        # never confirm one. Only /api/dependencies/{id}/confirm can.
        get_mod_row(mod_id, conn)
        get_mod_row(payload.source_mod_id, conn)
        dependency_id = dependencies.suggest_translation(mod_id, payload.source_mod_id, conn)
        return {"id": dependency_id}

    @app.post("/api/dependencies/{dependency_id}/confirm")
    def confirm_dependency_route(dependency_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        get_dependency_row(dependency_id, conn)
        dependencies.confirm_dependency(dependency_id, conn)
        return {"id": dependency_id, "confidence": "confirmed"}

    @app.post("/api/dependencies/{dependency_id}/reject")
    def reject_dependency_route(dependency_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        get_dependency_row(dependency_id, conn)
        dependencies.reject_dependency(dependency_id, conn)
        return {"rejected": dependency_id}

    @app.post("/api/mods/{mod_id}/enable")
    def enable_mod(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        get_mod_row(mod_id, conn)
        try:
            mod_manager.enable(mod_id, config=config, conn=conn)
        except dependencies.UnresolvedRequiredDependencyError as exc:
            logger.warning("Enable blocked for %r: unresolved required dependency", mod_id)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except mod_manager.ModManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info("Enabled mod %r", mod_id)
        return mod_summary(get_mod_row(mod_id, conn))

    @app.post("/api/mods/{mod_id}/disable")
    def disable_mod(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        get_mod_row(mod_id, conn)
        try:
            mod_manager.disable(mod_id, config=config, conn=conn)
        except mod_manager.ModManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info("Disabled mod %r", mod_id)
        return mod_summary(get_mod_row(mod_id, conn))

    @app.delete("/api/mods/{mod_id}")
    def delete_mod(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        get_mod_row(mod_id, conn)
        # Must run before mod_manager.delete() — see
        # profiles.record_missing_mod_if_saved()'s docstring for why this
        # can't be moved into mod_manager.delete() itself (it would also
        # fire for the internal delete-then-reinstall replace flows used by
        # download_watcher/broken_mods, which aren't real removals) or done
        # afterward (profile_mods' ON DELETE CASCADE erases the evidence).
        profiles_module.record_missing_mod_if_saved(mod_id, conn)
        try:
            mod_manager.delete(mod_id, config=config, conn=conn)
        except mod_manager.ModManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info("Deleted mod %r", mod_id)
        return {"deleted": mod_id}

    @app.post("/api/mods/{mod_id}/namespace-override")
    def set_namespace_override_route(
        mod_id: str, payload: SetNamespaceOverrideRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        # Corrects the Library's inferred grouping label for one mod — see
        # db.py's namespace_override column comment for why this is kept
        # separate from `author`. An empty/omitted value clears the
        # override, reverting to normal inference.
        try:
            mod_manager.set_namespace_override(mod_id, payload.value, conn=conn)
        except mod_manager.ModManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return mod_summary(get_mod_row(mod_id, conn))

    @app.post("/api/mods/{mod_id}/open-folder")
    def open_folder(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = get_mod_row(mod_id, conn)
        library_path = Path(row["library_path"])
        if library_path.is_dir():
            subprocess.Popen(["xdg-open", str(library_path)])  # best-effort, Linux-only
        return {"opened": str(library_path)}

    # --- Assisted Mode download detection -----------------------------------------

    @app.get("/api/downloads/pending")
    def list_pending_downloads() -> list[dict]:
        return pending_downloads.list()

    def pop_pending_or_404(token: str) -> dict:
        item = pending_downloads.pop(token)
        if item is None:
            raise HTTPException(status_code=404, detail=f"No pending download: {token}")
        return item

    @app.post("/api/downloads/{token}/install")
    def install_pending_download(token: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        item = pop_pending_or_404(token)
        try:
            mod_id = download_watcher.confirm_install(item["path"], config=config, conn=conn)
        except mod_manager.ModManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return mod_summary(get_mod_row(mod_id, conn))

    @app.post("/api/downloads/{token}/replace")
    def replace_pending_download(
        token: str, payload: ReplaceDownloadRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        item = pop_pending_or_404(token)
        try:
            new_mod_id = download_watcher.confirm_replace(
                item["path"], payload.mod_id, config=config, conn=conn
            )
        except (mod_manager.ModManagerError, download_watcher.DownloadWatcherError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return mod_summary(get_mod_row(new_mod_id, conn))

    @app.post("/api/downloads/{token}/dismiss")
    def dismiss_pending_download(token: str) -> dict:
        pop_pending_or_404(token)
        return {"dismissed": token}

    def build_metadata(
        curseforge_id: int, mod_info: curseforge.CurseForgeMod, latest: curseforge.CurseForgeFile
    ) -> mod_manager.ModMetadata:
        # Shared by install_from_catalog (new install) and apply_update
        # (replace) — both need the same fresh-metadata shape.
        return mod_manager.ModMetadata(
            curseforge_id=curseforge_id,
            author=mod_info.author,
            category=mod_info.category,
            installed_version=str(latest.file_id),
            compat_status=curseforge.compat_status(
                latest.game_version_min, latest.game_version_max, config.game_version
            ),
            short_description=mod_info.short_description,
            thumbnail_url=mod_info.thumbnail_url,
            links=json.dumps({"curseforge_url": mod_info.curseforge_url}) if mod_info.curseforge_url else None,
            game_version_min=latest.game_version_min,
            game_version_max=latest.game_version_max,
            third_party_distribution_allowed=mod_info.third_party_distribution_allowed,
        )

    def catalog_mod_dict(mod: curseforge.CurseForgeMod) -> dict:
        # No compat_status here on purpose: CurseForge search results don't
        # carry game-version ranges (only a file listing does), so the
        # catalog can't classify compatibility until a specific file is
        # picked at install time.
        return {
            "mod_id": mod.mod_id,
            "name": mod.name,
            "author": mod.author,
            "category": mod.category,
            "short_description": mod.short_description,
            "thumbnail_url": mod.thumbnail_url,
            "curseforge_url": mod.curseforge_url,
            "third_party_distribution_allowed": mod.third_party_distribution_allowed,
            "download_count": mod.download_count,
            "date_modified": mod.date_modified,
        }

    @app.get("/api/catalog/search")
    def search_catalog(
        q: str = "",
        sort: CatalogSort = "popularity",
        period: CatalogPeriod | None = None,
        index: int = 0,
    ) -> list[dict]:
        client = require_client()
        try:
            # Regression, found 2026-09-05: filtering by config.game_version
            # (the full auto-detected build string, e.g. "1.127.41.1030")
            # made every search return zero results — CurseForge's gameVersion
            # search filter needs an exact match against its own known
            # version-string list for the game, which this build string (or
            # even its truncated major.minor) never lands on. Confirmed live:
            # searching "MC Command Center" returned 0 hits with the filter,
            # 9 without. Compatibility is already computed separately, per
            # file, once a specific mod's files are looked at (see
            # catalog_mod_dict's own note) — this filter was never load-bearing
            # for that, so it's dropped rather than patched to a "better" guess.
            #
            # An empty q browses the full catalog unfiltered (confirmed live),
            # which is what powers the Catalog view's default landing state —
            # sort/period narrow that down without ever requiring a typed query.
            #
            # `index` (an offset, not a page number — matches CurseForge's own
            # param name/semantics) is what the frontend's infinite-scroll
            # advances on each call rather than replacing results; a single
            # page (default page_size) was the whole browsable catalog before.
            mods = client.search_mods(q, sort=sort, period=period, index=index)
        except curseforge.CurseForgeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return [catalog_mod_dict(mod) for mod in mods]

    @app.post("/api/catalog/{curseforge_mod_id}/install")
    def install_from_catalog(
        curseforge_mod_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        client = require_client()
        try:
            files = client.get_files(curseforge_mod_id)
            if not files:
                raise curseforge.CurseForgeError(f"No files available for mod {curseforge_mod_id}")
            latest = files[0]
            mod_info = client.get_mod(curseforge_mod_id)
            with tempfile.TemporaryDirectory(prefix="simslink-cf-") as tmp:
                downloaded = client.download(curseforge_mod_id, latest.file_id, Path(tmp) / latest.file_name)
                metadata = build_metadata(curseforge_mod_id, mod_info, latest)
                new_mod_id = mod_manager.install(
                    downloaded, config=config, conn=conn, mod_name=mod_info.name, metadata=metadata
                )
        except curseforge.CurseForgeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except mod_manager.ModManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info("Installed mod %r from catalog (CurseForge #%d)", new_mod_id, curseforge_mod_id)
        return mod_summary(get_mod_row(new_mod_id, conn))

    @app.post("/api/open-external")
    def open_external(payload: OpenExternalRequest) -> dict:
        # Opens in the system's default browser, never inside the app's own
        # pywebview window (CLAUDE.md's Assisted Mode step 1). Scheme is
        # restricted to http(s) so this can't be used to hand webbrowser.open
        # a file:// path or similar off a URL nothing here has vetted.
        if urlparse(payload.url).scheme not in ("http", "https"):
            raise HTTPException(status_code=400, detail="Only http(s) URLs can be opened externally")
        webbrowser.open(payload.url)
        return {"opened": payload.url}

    # --- Updates -----------------------------------------------------------------

    def mod_curseforge_url(row: sqlite3.Row) -> str | None:
        if not row["links"]:
            return None
        return (json.loads(row["links"]) or {}).get("curseforge_url")

    @app.post("/api/updates/check")
    def check_updates(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        # Direct Mode only, and only run on explicit request (this button
        # click) — never eagerly, since it's a network call per linked mod.
        client = require_client()
        rows = conn.execute(
            "SELECT * FROM mods WHERE curseforge_id IS NOT NULL ORDER BY name COLLATE NOCASE"
        ).fetchall()
        results = []
        for row in rows:
            try:
                files = client.get_files(row["curseforge_id"])
            except curseforge.CurseForgeError as exc:
                results.append({"id": row["id"], "name": row["name"], "status": "error", "error": str(exc)})
                continue
            if not files:
                continue
            latest = files[0]
            # Persisted regardless of update_available below — this is what
            # lets the Library's link-status indicator show "update
            # available" (mod_link_state() in main.py) without ever making a
            # live network call of its own; it just reflects the outcome of
            # this explicit, user-triggered check until the next one.
            conn.execute(
                "UPDATE mods SET latest_version = ?, latest_version_min = ?, latest_version_max = ? WHERE id = ?",
                (str(latest.file_id), latest.game_version_min, latest.game_version_max, row["id"]),
            )
            if str(latest.file_id) != (row["installed_version"] or ""):
                results.append(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "status": "update_available",
                        "latest_file_id": latest.file_id,
                        "latest_file_name": latest.file_name,
                    }
                )
            else:
                results.append({"id": row["id"], "name": row["name"], "status": "up_to_date"})
        conn.commit()
        return results

    @app.post("/api/updates/{mod_id}/apply")
    def apply_update(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        client = require_client()
        row = get_mod_row(mod_id, conn)
        curseforge_id = row["curseforge_id"]
        if curseforge_id is None:
            raise HTTPException(status_code=400, detail=f"'{mod_id}' has no known CurseForge link")
        try:
            files = client.get_files(curseforge_id)
            if not files:
                raise curseforge.CurseForgeError(f"No files available for mod {curseforge_id}")
            latest = files[0]
            mod_info = client.get_mod(curseforge_id)
            with tempfile.TemporaryDirectory(prefix="simslink-cf-update-") as tmp:
                downloaded = client.download(curseforge_id, latest.file_id, Path(tmp) / latest.file_name)
                metadata = build_metadata(curseforge_id, mod_info, latest)
                new_mod_id = download_watcher.confirm_replace(
                    downloaded, mod_id, config=config, conn=conn, metadata=metadata
                )
        except curseforge.CurseForgeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except (mod_manager.ModManagerError, download_watcher.DownloadWatcherError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info("Applied update for mod %r -> CurseForge file #%d", new_mod_id, latest.file_id)
        return mod_summary(get_mod_row(new_mod_id, conn))

    # --- Crash Mode ----------------------------------------------------------------

    def suspect_dict(suspect: crash_analyzer.Suspect) -> dict:
        return {"mod_id": suspect.mod_id, "confidence": suspect.confidence, "reason": suspect.reason}

    @app.post("/api/crash/analyze")
    def analyze_crash(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        exception_path = config.sims4_user_dir / "lastException.txt"
        if not exception_path.is_file():
            return {"found": False, "reports": []}
        raw = exception_path.read_text(encoding="utf-8", errors="replace")
        # lastException.txt can bundle several unrelated occurrences in one
        # file (see crash_analyzer's module docstring) — each becomes its
        # own crash_log row so suspects from unrelated incidents are never
        # mixed together.
        crash_log_ids = crash_analyzer.record_crash_reports(raw, conn=conn)
        return {
            "found": True,
            "reports": [
                {
                    "crash_log_id": crash_log_id,
                    "suspects": [suspect_dict(s) for s in crash_analyzer.get_suspects(crash_log_id, conn)],
                }
                for crash_log_id in crash_log_ids
            ],
        }

    @app.post("/api/crash/{crash_log_id}/bisection/start")
    def start_bisection(crash_log_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            disabled = crash_analyzer.start_bisection(crash_log_id, config=config, conn=conn)
        except crash_analyzer.CrashAnalyzerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"disabled": disabled}

    @app.post("/api/crash/{crash_log_id}/bisection/report")
    def report_bisection(
        crash_log_id: int, payload: BisectionReport, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        try:
            result = crash_analyzer.report_bisection_result(
                crash_log_id, payload.crash_occurred, config=config, conn=conn
            )
        except crash_analyzer.CrashAnalyzerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(result, list):
            return {"status": "next_round", "disabled": result}
        if result is not None:
            return {"status": "converged", "mod_id": result}
        return {"status": "inconclusive"}

    @app.post("/api/crash/{crash_log_id}/confirm-faulty")
    def confirm_faulty(
        crash_log_id: int, payload: ConfirmFaultyRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        # Records the user's explicit confirmation only — never deletes the
        # mod itself (CLAUDE.md: "Never silently auto-delete a mod based on
        # crash analysis"). Deleting it afterward is a separate Library action.
        crash_analyzer.confirm_faulty_mod(crash_log_id, payload.mod_id, conn)
        logger.warning("Mod %r confirmed as faulty for crash_log_id=%d", payload.mod_id, crash_log_id)
        return {"confirmed": payload.mod_id}

    @app.get("/api/cache/targets")
    def get_cache_targets() -> list[dict]:
        return [
            {"name": t.name, "description": t.description}
            for t in cache_cleaner.list_cache_targets(config)
            if t.exists
        ]

    @app.post("/api/cache/targets/{name}/open")
    def open_cache_target(name: str) -> dict:
        # `name` must match one of the known targets (list_cache_targets()'s
        # fixed spec, not arbitrary input) before it's ever joined onto
        # sims4_user_dir — same reasoning as open_folder() only ever opening
        # a mod's own library_path, never a caller-supplied path.
        if not any(t.name == name for t in cache_cleaner.list_cache_targets(config)):
            raise HTTPException(status_code=404, detail=f"No such cache target: {name}")
        path = config.sims4_user_dir / name
        if path.exists():
            # A directory target opens itself; a file target opens its
            # parent (sims4_user_dir) instead — xdg-open on a single file
            # launches whatever app is associated with it, which isn't
            # useful here, unlike reveal-in-file-manager for a folder.
            open_path = path if path.is_dir() else path.parent
            subprocess.Popen(["xdg-open", str(open_path)])  # best-effort, Linux-only
            return {"opened": str(open_path)}
        return {"opened": None}

    @app.post("/api/cache/clean")
    def clean_cache_route() -> dict:
        # No confirmation prompt here by design — that's the frontend's job
        # (CLAUDE.md: "always require confirmation before deleting"), this
        # route only executes once the user has already confirmed.
        cleaned = cache_cleaner.clean_cache(config)
        logger.info("Cleared cache targets: %s", cleaned)
        return {"cleaned": cleaned}

    # --- Settings ------------------------------------------------------------------

    @app.post("/api/settings/open-mods-folder")
    def open_mods_folder() -> dict:
        # Same "reveal in file manager" pattern as open_folder()/
        # open_cache_target()/open_broken_folder() — the header's "Open mods
        # folder" button. config.sims4_mods_dir is a property derived from
        # sims4_user_dir (see Config), so this always reads whatever's
        # currently configured, path-settings changes included.
        entry = config.sims4_mods_dir
        if entry.is_dir():
            subprocess.Popen(["xdg-open", str(entry)])  # best-effort, Linux-only
            return {"opened": str(entry)}
        return {"opened": None}

    @app.post("/api/settings/pick-folder")
    def pick_folder_route(payload: PickFolderRequest) -> dict:
        # Native OS folder picker for the path fields below, via zenity
        # (bundled with most GTK desktops, matching this project's existing
        # WebKitGTK/GTK stack) — run from the backend rather than the
        # browser since a web page can't resolve a chosen folder to a real
        # absolute filesystem path (see CLAUDE.md: desktop.py currently runs
        # in a plain browser tab, not a pywebview window with its own
        # native dialog bridge). Best-effort, Linux-only, same spirit as
        # every other xdg-open call in this file.
        args = ["zenity", "--file-selection", "--directory", "--title=SimsLink"]
        if payload.initial_dir:
            args.append(f"--filename={payload.initial_dir.rstrip('/')}/")
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=300)
        except FileNotFoundError:
            raise HTTPException(status_code=501, detail="No folder picker available (zenity not installed)")
        except subprocess.TimeoutExpired:
            return {"path": None}
        if result.returncode != 0:
            return {"path": None}  # cancelled
        return {"path": result.stdout.strip()}

    @app.get("/api/settings")
    def get_settings() -> dict:
        return {
            "game_dir": str(config.sims4_game_dir),
            "mods_dir": str(config.sims4_mods_dir),
            "user_dir": str(config.sims4_user_dir),
            "library_dir": str(config.library_dir),
            "download_watch_dir": str(config.download_watch_dir),
            "backup_retention_count": config.backup_retention_count,
            "mods_watcher_enabled": config.mods_watcher_enabled,
            "log_level": config.log_level,
            "log_path": str(config.log_path),
        }

    @app.post("/api/settings/paths")
    def update_paths_route(
        payload: UpdatePathsRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        # Reassigns the closed-over `config` used by every other route in
        # this function — see path_settings.py's module docstring for why
        # that's safe (routes always read it live) and what still needs a
        # restart (the real-time Mods/ watcher, which snapshots its target
        # directory once at .start() rather than reading config live).
        nonlocal config
        try:
            new_config, warnings = path_settings.update_paths(
                config,
                conn,
                sims4_game_dir=Path(payload.sims4_game_dir).expanduser() if payload.sims4_game_dir else None,
                sims4_user_dir=Path(payload.sims4_user_dir).expanduser() if payload.sims4_user_dir else None,
                library_dir=Path(payload.library_dir).expanduser() if payload.library_dir else None,
            )
        except path_settings.PathValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        config = new_config
        logger.info(
            "Updated installation paths: game_dir=%s user_dir=%s library_dir=%s",
            config.sims4_game_dir,
            config.sims4_user_dir,
            config.library_dir,
        )
        return {
            "game_dir": str(config.sims4_game_dir),
            "user_dir": str(config.sims4_user_dir),
            "library_dir": str(config.library_dir),
            "warnings": warnings,
            "restart_required": True,
        }

    @app.post("/api/settings/full-scan")
    def run_full_scan(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # Manual-only, synchronous — the caller (Settings button) is
        # expected to wait and show its own "scanning..." state; this isn't
        # the startup path CLAUDE.md's "must never block the UI" targets,
        # it's a deliberate, user-initiated, already-slow action (rehashes
        # every tracked file, parallelized across cores in scanner.py).
        # Also adopts any real, unmanaged folder dropped directly under
        # Mods/ since the app started — same reasoning as run_mods_rescan()
        # above: a "full" scan should mean full, not "only what's already
        # tracked."
        imported = scanner.import_untracked_mods(config, conn)
        stats = scanner.full_scan(config, conn)
        return {
            "mods_scanned": stats.mods_scanned,
            "mods_imported": len(imported),
            "files_hashed": stats.files_hashed,
            "files_unchanged": stats.files_unchanged,
            "files_removed": stats.files_removed,
        }

    @app.post("/api/settings/import-loose-files")
    def import_loose_files_route(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # Manual-only, same as Full scan above — see loose_mods.py's module
        # docstring for why a loose file at Mods/ root is never adopted
        # automatically by the watcher.
        imported = loose_mods.import_loose_files(config, conn)
        return {"mods_imported": len(imported)}

    def match_session_dict(session: curseforge_match.MatchSession) -> dict:
        return {
            "total": session.total,
            "checked": session.checked,
            "matched": session.matched,
            "skipped_too_large": session.skipped_too_large,
            "done": session.done,
        }

    @app.post("/api/settings/match-curseforge/start")
    def start_match_curseforge_route(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # Manual-only, Direct Mode only (require_client() raises 400 in
        # Assisted Mode, same check the catalog routes already use). Always
        # (re)builds the candidate list fresh — see curseforge_match.py's
        # module docstring for scope (is_loose_import mods with no
        # curseforge_id yet) and why a previous, possibly-stopped-early run
        # needs no explicit resume bookkeeping. Discards any prior in-
        # progress session — its work is already durably committed either
        # way (see run_step()).
        nonlocal match_session
        require_client()
        match_session = curseforge_match.start_session(conn)
        logger.info("CurseForge fingerprint match: starting, %d candidate(s)", match_session.total)
        return match_session_dict(match_session)

    @app.post("/api/settings/match-curseforge/step")
    def step_match_curseforge_route(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # One chunk (curseforge_match.CHUNK_SIZE mods) per call — the
        # frontend loops this to drive a live progress popup; stopping is
        # purely a frontend decision (just stop calling this) since every
        # step's matches are committed immediately, never batched up and
        # lost if the loop stops early.
        nonlocal match_session
        if match_session is None:
            raise HTTPException(
                status_code=400, detail="No CurseForge matching session in progress — call .../start first"
            )
        client = require_client()
        try:
            curseforge_match.run_step(match_session, conn, client)
        except curseforge.CurseForgeAuthError as exc:
            # The key itself was rejected mid-run (revoked/expired) —
            # retrying won't help, so this is deliberately *not* the
            # retryable 502 case below; fail the run outright.
            logger.warning("CurseForge fingerprint match: key rejected mid-run — %s", exc)
            match_session = None
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except (curseforge.CurseForgeError, requests.RequestException) as exc:
            # Transient (network hiccup, a slow/rate-limited response, ...)
            # — run_step() leaves `match_session` completely untouched on
            # failure, so retrying this exact call retries the exact same
            # chunk. 502 (not 500) specifically so the frontend can tell
            # "the run should retry with backoff" apart from a real bug.
            logger.warning("CurseForge fingerprint match: step failed, retryable — %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        result = match_session_dict(match_session)
        if match_session.done:
            logger.info(
                "CurseForge fingerprint match: done — %d checked, %d matched, %d skipped (too large)",
                match_session.checked,
                match_session.matched,
                match_session.skipped_too_large,
            )
            match_session = None
        return result

    def sync_session_dict(session: curseforge_dependencies.SyncSession) -> dict:
        return {
            "total": session.total,
            "checked": session.checked,
            "synced": session.synced,
            "errors": session.errors,
            "done": session.done,
        }

    @app.post("/api/settings/sync-curseforge/start")
    def start_sync_curseforge_route(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # Manual-only, Direct Mode only. Always (re)builds the candidate
        # list fresh — every currently-linked mod, no "last synced"
        # bookkeeping (see curseforge_dependencies.start_sync_session()).
        # Discards any prior in-progress session — its work is already
        # durably committed either way (see run_sync_step()).
        nonlocal sync_session
        require_client()
        sync_session = curseforge_dependencies.start_sync_session(conn)
        logger.info("CurseForge sync: starting, %d linked mod(s)", sync_session.total)
        return sync_session_dict(sync_session)

    @app.post("/api/settings/sync-curseforge/step")
    def step_sync_curseforge_route(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # One chunk (curseforge_dependencies.SYNC_CHUNK_SIZE mods) per call —
        # the frontend loops this to drive a live progress popup. Unlike
        # match-curseforge's step, a single mod's transient failure inside
        # run_sync_step() is already caught and skipped per-mod (see its own
        # docstring) rather than failing the whole chunk, so there's no
        # retryable-502 case here to handle client-side — only the
        # not-retryable auth failure below.
        nonlocal sync_session
        if sync_session is None:
            raise HTTPException(
                status_code=400, detail="No CurseForge sync session in progress — call .../start first"
            )
        client = require_client()
        try:
            curseforge_dependencies.run_sync_step(sync_session, conn, client, config.game_version)
        except curseforge.CurseForgeAuthError as exc:
            logger.warning("CurseForge sync: key rejected mid-run — %s", exc)
            sync_session = None
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        result = sync_session_dict(sync_session)
        if sync_session.done:
            logger.info(
                "CurseForge sync: done — %d checked, %d synced, %d error(s)",
                sync_session.checked,
                sync_session.synced,
                sync_session.errors,
            )
            sync_session = None
        return result

    # --- Compat quarantine (disable incompatible mods + local required dependents) ---

    def quarantine_candidate_dict(candidate: compat_quarantine.QuarantineCandidate) -> dict:
        return {"mod_id": candidate.mod_id, "name": candidate.name, "reason": candidate.reason}

    @app.get("/api/compat/quarantine/preview")
    def preview_compat_quarantine_route(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        return [quarantine_candidate_dict(c) for c in compat_quarantine.preview_quarantine(conn)]

    @app.post("/api/compat/quarantine")
    def quarantine_mods_route(
        payload: QuarantineModsRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        # Re-derives the candidate set server-side and only ever acts on the
        # intersection with what the caller confirmed — the preview's
        # `reason` is computed here, never trusted from the request body,
        # and a stale/unrelated mod_id slipped into the payload is silently
        # ignored rather than disabled.
        requested = set(payload.mod_ids)
        candidates = [c for c in compat_quarantine.preview_quarantine(conn) if c.mod_id in requested]
        quarantined = compat_quarantine.quarantine_mods(candidates, config=config, conn=conn)
        if quarantined:
            logger.info("Compat quarantine: disabled %d mod(s): %s", len(quarantined), quarantined)
        return {"quarantined": quarantined}

    @app.get("/api/compat/quarantine")
    def list_compat_quarantine_route(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        return compat_quarantine.list_quarantined(conn)

    @app.post("/api/compat/quarantine/release")
    def release_compat_quarantine_route(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        result = compat_quarantine.release_ready_mods(config=config, conn=conn)
        if result["released"]:
            logger.info("Compat quarantine: re-enabled %d mod(s): %s", len(result["released"]), result["released"])
        return result

    @app.delete("/api/compat/quarantine/{mod_id}")
    def forget_compat_quarantine_route(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        compat_quarantine.forget_quarantined(mod_id, conn)
        return {"forgotten": mod_id}

    # --- Profiles --------------------------------------------------------------------

    def profile_dict(profile: profiles_module.Profile) -> dict:
        return {
            "id": profile.id,
            "name": profile.name,
            "mod_ids": profile.mod_ids,
            "created_date": profile.created_date,
        }

    @app.get("/api/profiles")
    def list_profiles_route(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        return [profile_dict(p) for p in profiles_module.list_profiles(conn)]

    @app.post("/api/profiles")
    def create_profile_route(payload: CreateProfileRequest, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            profile_id = profiles_module.create_profile(payload.name, conn)
        except profiles_module.ProfileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return profile_dict(profiles_module.get_profile(profile_id, conn))

    @app.delete("/api/profiles/{profile_id}")
    def delete_profile_route(profile_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        profiles_module.delete_profile(profile_id, conn)
        return {"deleted": profile_id}

    @app.put("/api/profiles/{profile_id}/mods")
    def set_profile_mods_route(
        profile_id: int, payload: SetProfileModsRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        try:
            profiles_module.set_profile_mods(profile_id, payload.mod_ids, conn)
        except profiles_module.ProfileError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return profile_dict(profiles_module.get_profile(profile_id, conn))

    @app.post("/api/profiles/{profile_id}/activate")
    def activate_profile_route(profile_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            profiles_module.activate_profile(profile_id, config=config, conn=conn)
        except profiles_module.ProfileError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except dependencies.UnresolvedRequiredDependencyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except mod_manager.ModManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info("Activated profile %d", profile_id)
        return profile_dict(profiles_module.get_profile(profile_id, conn))

    # --- Blacklist -------------------------------------------------------------------

    def blacklist_entry_dict(entry: blacklist_module.BlacklistEntry) -> dict:
        return {"id": entry.id, "pattern": entry.pattern, "note": entry.note}

    @app.get("/api/blacklist")
    def list_blacklist_route(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        return [blacklist_entry_dict(e) for e in blacklist_module.list_entries(conn)]

    @app.post("/api/blacklist")
    def add_blacklist_entry_route(
        payload: AddBlacklistEntryRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        try:
            entry_id = blacklist_module.add_entry(payload.pattern, conn, note=payload.note)
        except blacklist_module.BlacklistError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        entry = next(e for e in blacklist_module.list_entries(conn) if e.id == entry_id)
        return blacklist_entry_dict(entry)

    @app.delete("/api/blacklist/{entry_id}")
    def remove_blacklist_entry_route(entry_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        blacklist_module.remove_entry(entry_id, conn)
        return {"deleted": entry_id}

    @app.get("/api/blacklist/matches")
    def blacklist_matches_route(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        # Only currently-active mods: a disabled mod's files aren't loaded by
        # the game, so a blacklist match against it isn't a live concern —
        # same reasoning as conflict_detector.py excluding disabled mods.
        entries = blacklist_module.list_entries(conn)
        if not entries:
            return []
        results = []
        for row in conn.execute("SELECT id, name FROM mods WHERE active = 1 ORDER BY name COLLATE NOCASE"):
            hits = blacklist_module.find_matches(row["name"], row["id"], entries)
            if hits:
                results.append(
                    {
                        "mod_id": row["id"],
                        "mod_name": row["name"],
                        "patterns": [h.pattern for h in hits],
                        "pattern_ids": [h.id for h in hits],
                    }
                )
        return results

    # Registered last so it never shadows the /api/ routes above.
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app
