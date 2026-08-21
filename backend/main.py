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
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import blacklist as blacklist_module
from . import cache_cleaner
from . import conflict_detector
from . import crash_analyzer
from . import curseforge
from . import db
from . import dependencies
from . import download_watcher
from . import game_options
from . import mod_manager
from . import profiles as profiles_module
from . import scanner
from .config import Config

APP_VERSION = "0.1.0"  # keep in sync with pyproject.toml's [project].version
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

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

    def run_incremental_scan() -> None:
        conn = db.connect(db_path)
        try:
            scanner.incremental_scan(config, conn)
        finally:
            conn.close()

    def schedule_mods_rescan() -> None:
        nonlocal _scan_timer
        with _scan_debounce_lock:
            if _scan_timer is not None:
                _scan_timer.cancel()
            _scan_timer = threading.Timer(_SCAN_DEBOUNCE_SECONDS, run_incremental_scan)
            _scan_timer.daemon = True
            _scan_timer.start()

    def run_startup_scan() -> None:
        # Adopts anything dropped directly under Mods/ before the app
        # managed it, then catches up on size/mtime changes made while the
        # app was closed — the real-time watcher only covers "while running".
        conn = db.connect(db_path)
        try:
            scanner.import_untracked_mods(config, conn)
            scanner.incremental_scan(config, conn)
        finally:
            conn.close()

    app.state.mods_watcher = scanner.ModsFolderWatcher(config, schedule_mods_rescan)
    app.state.run_startup_scan = run_startup_scan
    app.state.schedule_mods_rescan = schedule_mods_rescan  # exposed for tests, same idea as report_download

    def mod_summary(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "author": row["author"],
            "category": row["category"],
            "primary_type": row["primary_type"],
            "installed_version": row["installed_version"],
            "compat_status": row["compat_status"],
            "active": bool(row["active"]),
            "short_description": row["short_description"],
            "thumbnail_url": row["thumbnail_url"],
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
        # to {id, name} here so the frontend never has to look them up itself.
        results = []
        for group in conflict_detector.find_conflicts(conn):
            mods = []
            for mod_id in group.mod_ids:
                row = conn.execute("SELECT name FROM mods WHERE id = ?", (mod_id,)).fetchone()
                mods.append({"id": mod_id, "name": row["name"] if row is not None else mod_id})
            results.append({"kind": group.kind, "identifier": group.identifier, "mods": mods})
        return results

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
        }

    def get_dependency_row(dependency_id: int, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM dependencies WHERE id = ?", (dependency_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No such dependency: {dependency_id}")
        return row

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
        try:
            mod_manager.delete(mod_id, config=config, conn=conn)
        except mod_manager.ModManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info("Deleted mod %r", mod_id)
        return {"deleted": mod_id}

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
        }

    @app.get("/api/catalog/search")
    def search_catalog(q: str = "") -> list[dict]:
        client = require_client()
        try:
            mods = client.search_mods(q, game_version=config.game_version)
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

    @app.get("/api/updates/checklist")
    def updates_checklist(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        # Assisted Mode's manual checklist: every installed mod with a known
        # CurseForge URL, regardless of mode — harmless to expose in Direct
        # Mode too, the frontend just doesn't render it there.
        rows = conn.execute("SELECT * FROM mods ORDER BY name COLLATE NOCASE").fetchall()
        checklist = []
        for row in rows:
            url = mod_curseforge_url(row)
            if url:
                checklist.append({"id": row["id"], "name": row["name"], "curseforge_url": url})
        return checklist

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
            return {"found": False, "crash_log_id": None, "suspects": []}
        raw = exception_path.read_text(encoding="utf-8", errors="replace")
        crash_log_id = crash_analyzer.record_crash(raw, conn=conn)
        suspects = crash_analyzer.get_suspects(crash_log_id, conn)
        return {
            "found": True,
            "crash_log_id": crash_log_id,
            "suspects": [suspect_dict(s) for s in suspects],
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

    @app.post("/api/cache/clean")
    def clean_cache_route() -> dict:
        # No confirmation prompt here by design — that's the frontend's job
        # (CLAUDE.md: "always require confirmation before deleting"), this
        # route only executes once the user has already confirmed.
        cleaned = cache_cleaner.clean_cache(config)
        logger.info("Cleared cache targets: %s", cleaned)
        return {"cleaned": cleaned}

    # --- Settings ------------------------------------------------------------------

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

    @app.post("/api/settings/full-scan")
    def run_full_scan(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        # Manual-only, synchronous — the caller (Settings button) is
        # expected to wait and show its own "scanning..." state; this isn't
        # the startup path CLAUDE.md's "must never block the UI" targets,
        # it's a deliberate, user-initiated, already-slow action (rehashes
        # every tracked file, parallelized across cores in scanner.py).
        stats = scanner.full_scan(config, conn)
        return {
            "mods_scanned": stats.mods_scanned,
            "files_hashed": stats.files_hashed,
            "files_unchanged": stats.files_unchanged,
            "files_removed": stats.files_removed,
        }

    # --- Profiles --------------------------------------------------------------------

    def profile_dict(profile: profiles_module.Profile) -> dict:
        return {"id": profile.id, "name": profile.name, "mod_ids": profile.mod_ids}

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
        entries = blacklist_module.list_entries(conn)
        if not entries:
            return []
        results = []
        for row in conn.execute("SELECT id, name FROM mods ORDER BY name COLLATE NOCASE"):
            hits = blacklist_module.find_matches(row["name"], row["id"], entries)
            if hits:
                results.append(
                    {"mod_id": row["id"], "mod_name": row["name"], "patterns": [h.pattern for h in hits]}
                )
        return results

    # Registered last so it never shadows the /api/ routes above.
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app
