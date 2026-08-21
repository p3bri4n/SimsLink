"""Tests backend/main.py's routes via FastAPI's TestClient — no real HTTP
server, no browser (see CLAUDE.md's Testing section). Route tests stay thin:
assert the route calls the right mod_manager.py/dependencies.py function and
translates its result/errors into the right HTTP status/JSON — the business
logic itself is already covered by tests/test_mod_manager.py,
tests/test_dependencies.py, etc.

Named test_backend_main.py rather than test_main.py to avoid colliding with
tests/test_main.py, which covers the pre-migration Flet main.py (see
CLAUDE.md's "Current project status" — both apps coexist during migration).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dependencies as deps
import mod_manager
from backend.main import create_app


@pytest.fixture
def client(app_config, tmp_path) -> TestClient:
    # Same tmp_path/"simslink.sqlite3" the `conn` fixture (conftest.py) uses,
    # so mods installed via `conn` in a test are visible through the app's
    # own connections — config.db_path itself is a fixed real XDG path (see
    # config.py), not something a test should point the app at.
    return TestClient(create_app(app_config, db_path=tmp_path / "simslink.sqlite3"))


def _install_mod(app_config, conn, tmp_path, name, filename="mymod.package", content=b"data") -> str:
    archive = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename, content)
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name)


# --- /api/status ---------------------------------------------------------------


def test_status_reports_assisted_mode_without_api_key(client):
    response = client.get("/api/status")

    assert response.status_code == 200
    body = response.json()
    assert body["direct_mode"] is False
    assert body["app_version"]


# --- /api/mods (list) ------------------------------------------------------------


def test_list_mods_empty(client):
    response = client.get("/api/mods")

    assert response.status_code == 200
    assert response.json() == []


def test_list_mods_returns_installed_mods(app_config, conn, tmp_path, client):
    _install_mod(app_config, conn, tmp_path, "Better Woohoo")

    response = client.get("/api/mods")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "Better Woohoo"
    assert body[0]["active"] is True


# --- /api/mods/{id} (detail) -----------------------------------------------------


def test_get_mod_404_for_unknown_id(client):
    response = client.get("/api/mods/does-not-exist")

    assert response.status_code == 404


def test_get_mod_detail_includes_files_and_resolved_dependency(app_config, conn, tmp_path, client):
    core_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    mod_id = _install_mod(app_config, conn, tmp_path, "Needs Core", filename="needs_core.package")
    deps.add_dependency(mod_id, conn=conn, dependency_type="required", depends_on_mod_id=core_id)

    response = client.get(f"/api/mods/{mod_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["files"] == ["needs_core.package"]
    assert len(body["dependencies"]) == 1
    dep = body["dependencies"][0]
    assert dep["resolved_name"] == "Core Lib"
    assert dep["dependency_type"] == "required"
    assert dep["confidence"] == "confirmed"


def test_get_mod_detail_dependency_falls_back_to_curseforge_id_when_unresolved_locally(
    app_config, conn, tmp_path, client
):
    mod_id = _install_mod(app_config, conn, tmp_path, "Needs External")
    deps.add_dependency(
        mod_id, conn=conn, dependency_type="required", depends_on_curseforge_id=999, mandatory=False
    )

    response = client.get(f"/api/mods/{mod_id}")

    dep = response.json()["dependencies"][0]
    assert dep["resolved_name"] is None
    assert dep["depends_on_curseforge_id"] == 999


# --- enable / disable ------------------------------------------------------------


def test_disable_then_enable_roundtrip(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Toggle Me")

    disable_resp = client.post(f"/api/mods/{mod_id}/disable")
    assert disable_resp.status_code == 200
    assert disable_resp.json()["active"] is False

    enable_resp = client.post(f"/api/mods/{mod_id}/enable")
    assert enable_resp.status_code == 200
    assert enable_resp.json()["active"] is True


def test_enable_blocked_by_unresolved_required_dependency_returns_409(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Needs Core")
    mod_manager.disable(mod_id, config=app_config, conn=conn)
    core_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    mod_manager.disable(core_id, config=app_config, conn=conn)
    deps.add_dependency(mod_id, conn=conn, dependency_type="required", depends_on_mod_id=core_id)

    response = client.post(f"/api/mods/{mod_id}/enable")

    assert response.status_code == 409


def test_enable_unknown_mod_returns_404(client):
    response = client.post("/api/mods/does-not-exist/enable")

    assert response.status_code == 404


# --- delete ------------------------------------------------------------------------


def test_delete_removes_mod_from_db_and_library(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Delete Me")
    library_path = Path(app_config.library_dir / mod_id)
    assert library_path.exists()

    response = client.delete(f"/api/mods/{mod_id}")

    assert response.status_code == 200
    assert response.json() == {"deleted": mod_id}
    assert conn.execute("SELECT 1 FROM mods WHERE id = ?", (mod_id,)).fetchone() is None
    assert not library_path.exists()


# --- open-folder -------------------------------------------------------------------


def test_open_folder_invokes_xdg_open_on_the_library_path(app_config, conn, tmp_path, client, monkeypatch):
    mod_id = _install_mod(app_config, conn, tmp_path, "Open Me")
    calls = []
    monkeypatch.setattr("backend.main.subprocess.Popen", lambda args: calls.append(args))

    response = client.post(f"/api/mods/{mod_id}/open-folder")

    assert response.status_code == 200
    assert calls == [["xdg-open", str(app_config.library_dir / mod_id)]]


def test_open_folder_unknown_mod_returns_404(client):
    response = client.post("/api/mods/does-not-exist/open-folder")

    assert response.status_code == 404


# --- static frontend mount ----------------------------------------------------------


def test_root_serves_frontend_index(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "SimsLink" in response.text
