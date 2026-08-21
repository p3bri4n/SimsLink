"""FastAPI backend — first vertical slice of the Flet-to-FastAPI/pywebview
migration (see CLAUDE.md's "Architecture" and "Current project status").
Only the Library view (list/detail/enable/disable/delete/open-folder) is
wired up; Catalog/Updates/Crash Mode/Settings still live only in the Flet
app (`ui/*.py`) for now.

Serves `frontend/` as static files too, so the whole app is reachable at
http://localhost:8000/ with a single origin — no CORS needed. Everything
under /api/ is JSON; every other path falls through to the static mount.

Business logic is untouched by this migration: this module only translates
HTTP <-> the existing mod_manager.py/dependencies.py/db.py/config.py, which
still live at the project root (not yet moved under backend/, see CLAUDE.md)
and are importable here because desktop.py — the process entry point — also
lives at the project root, putting it on sys.path for the whole process.

`create_app()` takes a `Config` instead of resolving one from the
environment at import time, specifically so tests can build an app against a
throwaway temp Config/DB via FastAPI's TestClient, the same way the Flet
`main.py` defers `Config.from_env()` into `main(page)` for testability.
"""

from __future__ import annotations

import subprocess
import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

import curseforge
import db
import dependencies
import mod_manager
from config import Config

APP_VERSION = "0.1.0"  # keep in sync with pyproject.toml's [project].version
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def create_app(config: Config, *, db_path: Path | None = None) -> FastAPI:
    """`db_path` defaults to `config.db_path` (the real, fixed XDG data
    location — see config.py) for production use. Tests override it to an
    isolated tmp_path database instead of touching that shared real file,
    the same way tests/conftest.py's `conn` fixture already does."""
    db_path = db_path or config.db_path
    app = FastAPI(title="SimsLink")
    db.init_db(db_path)

    # Resolved once at app creation, not per-request — verify_key() is a
    # network call. Mirrors main.py's (Flet) Direct/Assisted resolution.
    direct_mode = False
    if config.has_api_key:
        candidate = curseforge.CurseForgeClient(config.curseforge_api_key)
        direct_mode = candidate.verify_key()

    def get_conn():
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

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
        return {
            "app_version": APP_VERSION,
            "game_version": config.game_version,
            "direct_mode": direct_mode,
        }

    @app.get("/api/mods")
    def list_mods(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        rows = conn.execute("SELECT * FROM mods ORDER BY name COLLATE NOCASE").fetchall()
        return [mod_summary(row) for row in rows]

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

    @app.post("/api/mods/{mod_id}/enable")
    def enable_mod(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        get_mod_row(mod_id, conn)
        try:
            mod_manager.enable(mod_id, config=config, conn=conn)
        except dependencies.UnresolvedRequiredDependencyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except mod_manager.ModManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return mod_summary(get_mod_row(mod_id, conn))

    @app.post("/api/mods/{mod_id}/disable")
    def disable_mod(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        get_mod_row(mod_id, conn)
        try:
            mod_manager.disable(mod_id, config=config, conn=conn)
        except mod_manager.ModManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return mod_summary(get_mod_row(mod_id, conn))

    @app.delete("/api/mods/{mod_id}")
    def delete_mod(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        get_mod_row(mod_id, conn)
        try:
            mod_manager.delete(mod_id, config=config, conn=conn)
        except mod_manager.ModManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"deleted": mod_id}

    @app.post("/api/mods/{mod_id}/open-folder")
    def open_folder(mod_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = get_mod_row(mod_id, conn)
        library_path = Path(row["library_path"])
        if library_path.is_dir():
            subprocess.Popen(["xdg-open", str(library_path)])  # best-effort, Linux-only
        return {"opened": str(library_path)}

    # Registered last so it never shadows the /api/ routes above.
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app
