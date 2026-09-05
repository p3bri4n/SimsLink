"""Tests backend/main.py's routes via FastAPI's TestClient — no real HTTP
server, no browser (see CLAUDE.md's Testing section). Route tests stay thin:
assert the route calls the right mod_manager.py/dependencies.py function and
translates its result/errors into the right HTTP status/JSON — the business
logic itself is already covered by tests/test_mod_manager.py,
tests/test_dependencies.py, etc.
"""

from __future__ import annotations

import dataclasses
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import curseforge
from backend import dependencies as deps
from backend import mod_manager
from backend import scanner
from backend.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def app(app_config, tmp_path):
    # Same tmp_path/"simslink.sqlite3" the `conn` fixture (conftest.py) uses,
    # so mods installed via `conn` in a test are visible through the app's
    # own connections — config.db_path itself is a fixed real XDG path (see
    # config.py), not something a test should point the app at.
    return create_app(app_config, db_path=tmp_path / "simslink.sqlite3")


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


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


def test_status_reports_script_mods_allowed(app_config, client):
    (app_config.sims4_user_dir / "options.ini").write_text("scriptmodsenabled=1\n")

    response = client.get("/api/status")

    assert response.json()["script_mods_allowed"] is True


def test_status_reports_script_mods_allowed_none_without_options_ini(client):
    response = client.get("/api/status")

    assert response.json()["script_mods_allowed"] is None


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


# --- /api/conflicts ---------------------------------------------------------------


def test_conflicts_empty_when_no_duplicates(app_config, conn, tmp_path, client):
    _install_mod(app_config, conn, tmp_path, "Solo Mod")

    response = client.get("/api/conflicts")

    assert response.status_code == 200
    assert response.json() == []


def test_conflicts_reports_package_duplicate_with_resolved_names(app_config, conn, tmp_path, client):
    _install_mod(app_config, conn, tmp_path, "Mod A", filename="shared.package", content=b"same-bytes")
    # Mod B also has a file of its own, so its full file set doesn't happen
    # to exactly match Mod A's — that's exact_duplicate_mod's signal
    # (tested separately below), this test is about the plain
    # single-shared-file duplicate_package case.
    archive = tmp_path / "Mod B.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("shared.package", b"same-bytes")
        zf.writestr("extra.package", b"only-in-b")
    mod_manager.install(archive, config=app_config, conn=conn, mod_name="Mod B")

    response = client.get("/api/conflicts")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["kind"] == "duplicate_package"
    assert body[0]["file_count"] == 1
    names = sorted(m["name"] for m in body[0]["mods"])
    assert names == ["Mod A", "Mod B"]
    for m in body[0]["mods"]:
        assert m["active"] is True
        assert m["install_date"]
        assert "author" in m


def test_conflicts_reports_exact_duplicate_mod(app_config, conn, tmp_path, client):
    _install_mod(app_config, conn, tmp_path, "Mod A", filename="shared.package", content=b"same-bytes")
    _install_mod(app_config, conn, tmp_path, "Mod B", filename="shared.package", content=b"same-bytes")

    response = client.get("/api/conflicts")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["kind"] == "exact_duplicate_mod"
    assert body[0]["file_count"] == 1
    names = sorted(m["name"] for m in body[0]["mods"])
    assert names == ["Mod A", "Mod B"]


def test_conflicts_reports_ts4script_name_collision(app_config, conn, tmp_path, client):
    _install_mod(app_config, conn, tmp_path, "Mod A", filename="core.ts4script", content=b"content-a")
    _install_mod(app_config, conn, tmp_path, "Mod B", filename="core.ts4script", content=b"different-content-b")

    response = client.get("/api/conflicts")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["kind"] == "ts4script_name_collision"
    assert body[0]["identifier"] == "core.ts4script"


# --- /api/mods/broken -------------------------------------------------------------


def test_broken_mods_empty_when_nothing_unmanaged(app_config, conn, tmp_path, client):
    _install_mod(app_config, conn, tmp_path, "Solo Mod")

    response = client.get("/api/mods/broken")

    assert response.status_code == 200
    assert response.json() == []


def test_broken_mods_reports_classified_folders(app_config, client):
    (app_config.sims4_mods_dir / "ExtractedScript" / "pkg").mkdir(parents=True)
    (app_config.sims4_mods_dir / "ExtractedScript" / "pkg" / "main.pyc").write_bytes(b"x")

    response = client.get("/api/mods/broken")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0] == {
        "name": "ExtractedScript",
        "reason": "unpacked_script",
        "file_count": 1,
        "zip_paths": [],
        "sample_files": ["pkg/main.pyc"],
    }


def test_fix_broken_mod_empty_folder(app_config, client):
    (app_config.sims4_mods_dir / "OptionalAddons").mkdir()

    response = client.post("/api/mods/broken/OptionalAddons/fix")

    assert response.status_code == 200
    assert response.json() == {"fixed": True, "mod_id": None}
    assert not (app_config.sims4_mods_dir / "OptionalAddons").exists()


def test_fix_broken_mod_unextracted_archive(app_config, client):
    folder = app_config.sims4_mods_dir / "SomeMod"
    folder.mkdir()
    with zipfile.ZipFile(folder / "SomeMod.zip", "w") as zf:
        zf.writestr("mymod.package", b"data")

    response = client.post("/api/mods/broken/SomeMod/fix")

    assert response.status_code == 200
    body = response.json()
    assert body["fixed"] is True
    assert body["mod_id"]
    assert not folder.exists()


def test_fix_broken_mod_unsupported_reason_returns_400(app_config, client):
    folder = app_config.sims4_mods_dir / "Leftovers"
    folder.mkdir()
    (folder / "notes.log").write_bytes(b"log")

    response = client.post("/api/mods/broken/Leftovers/fix")

    assert response.status_code == 400
    assert folder.exists()


def test_extract_zips_installs_selected_archives(app_config, client):
    folder = app_config.sims4_mods_dir / "ChooseOne"
    folder.mkdir()
    with zipfile.ZipFile(folder / "OptionA.zip", "w") as zf:
        zf.writestr("a.package", b"a")
    with zipfile.ZipFile(folder / "OptionB.zip", "w") as zf:
        zf.writestr("b.package", b"b")

    response = client.post("/api/mods/broken/ChooseOne/extract-zips", json={"zip_paths": ["OptionA.zip"]})

    assert response.status_code == 200
    body = response.json()
    assert len(body["installed"]) == 1
    assert body["deferred"] == []
    assert not folder.exists()


def test_extract_zips_empty_selection_returns_400(app_config, client):
    folder = app_config.sims4_mods_dir / "ChooseOne"
    folder.mkdir()
    with zipfile.ZipFile(folder / "OptionA.zip", "w") as zf:
        zf.writestr("a.package", b"a")

    response = client.post("/api/mods/broken/ChooseOne/extract-zips", json={"zip_paths": []})

    assert response.status_code == 400
    assert folder.exists()


def test_attempt_script_repair_installs_new_mod(app_config, client):
    folder = app_config.sims4_mods_dir / "ExtractedScript"
    folder.mkdir()
    (folder / "main.pyc").write_bytes(b"compiled")

    response = client.post("/api/mods/broken/ExtractedScript/attempt-script-repair")

    assert response.status_code == 200
    body = response.json()
    assert body["repaired"] is True
    assert body["mod_id"]
    assert not folder.exists()


def test_attempt_script_repair_wrong_reason_returns_400(app_config, client):
    folder = app_config.sims4_mods_dir / "OptionalAddons"
    folder.mkdir()

    response = client.post("/api/mods/broken/OptionalAddons/attempt-script-repair")

    assert response.status_code == 400
    assert folder.exists()


def test_delete_broken_folder_removes_it(app_config, client):
    folder = app_config.sims4_mods_dir / "ExtractedScript"
    folder.mkdir()
    (folder / "main.pyc").write_bytes(b"compiled")

    response = client.delete("/api/mods/broken/ExtractedScript")

    assert response.status_code == 200
    assert response.json() == {"deleted": "ExtractedScript"}
    assert not folder.exists()


def test_delete_broken_folder_unknown_returns_400(client):
    response = client.delete("/api/mods/broken/DoesNotExist")

    assert response.status_code == 400


def test_open_broken_folder_invokes_xdg_open(app_config, client, monkeypatch):
    (app_config.sims4_mods_dir / "Leftovers").mkdir()
    calls = []
    monkeypatch.setattr("backend.main.subprocess.Popen", lambda args: calls.append(args))

    response = client.post("/api/mods/broken/Leftovers/open")

    assert response.status_code == 200
    assert calls == [["xdg-open", str(app_config.sims4_mods_dir / "Leftovers")]]


def test_open_broken_folder_unknown_returns_404(client):
    response = client.post("/api/mods/broken/DoesNotExist/open")

    assert response.status_code == 404


# --- /api/mods/rezipped ("dezip via the app, rezip manually" scenario) ---------------


def test_rezipped_mods_empty_when_nothing_rezipped(app_config, conn, client, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "RealMod")

    response = client.get("/api/mods/rezipped")

    assert response.status_code == 200
    assert response.json() == []
    assert mod_id  # sanity: install actually happened


def test_rezipped_mods_reports_a_mod_manually_rezipped_in_place(app_config, conn, client, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "RealMod")
    library_path = Path(
        conn.execute("SELECT library_path FROM mods WHERE id = ?", (mod_id,)).fetchone()["library_path"]
    )
    for f in library_path.rglob("*"):
        if f.is_file():
            f.unlink()
    with zipfile.ZipFile(library_path / "Rezipped.zip", "w") as zf:
        zf.writestr("rezipped.package", b"data")

    response = client.get("/api/mods/rezipped")

    assert response.status_code == 200
    body = response.json()
    assert body == [{"mod_id": mod_id, "name": "RealMod", "zip_paths": ["Rezipped.zip"]}]


def test_fix_rezip_reinstalls_from_the_zip(app_config, conn, client, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "RealMod")
    library_path = Path(
        conn.execute("SELECT library_path FROM mods WHERE id = ?", (mod_id,)).fetchone()["library_path"]
    )
    for f in library_path.rglob("*"):
        if f.is_file():
            f.unlink()
    with zipfile.ZipFile(library_path / "Rezipped.zip", "w") as zf:
        zf.writestr("rezipped.package", b"data")

    response = client.post(f"/api/mods/{mod_id}/fix-rezip")

    assert response.status_code == 200
    body = response.json()
    assert body["fixed"] is True
    assert body["mod_id"] == mod_id
    new_library_path = Path(
        conn.execute("SELECT library_path FROM mods WHERE id = ?", (mod_id,)).fetchone()["library_path"]
    )
    assert (new_library_path / "rezipped.package").is_file()


def test_fix_rezip_unknown_mod_returns_400(client):
    response = client.post("/api/mods/does-not-exist/fix-rezip")

    assert response.status_code == 400


def test_fix_rezip_not_actually_rezipped_returns_400(app_config, conn, client, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "RealMod")

    response = client.post(f"/api/mods/{mod_id}/fix-rezip")

    assert response.status_code == 400


# --- /api/mods/{id} (detail) -----------------------------------------------------


def test_get_mod_404_for_unknown_id(client):
    response = client.get("/api/mods/does-not-exist")

    assert response.status_code == 404


def test_curseforge_name_exposed_in_list_and_detail(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "messy-filename-name", filename="messy.package")
    conn.execute(
        "UPDATE mods SET curseforge_id = 123, curseforge_name = 'The Real Mod Name' WHERE id = ?", (mod_id,)
    )
    conn.commit()

    listed = next(m for m in client.get("/api/mods").json() if m["id"] == mod_id)
    detail = client.get(f"/api/mods/{mod_id}").json()

    assert listed["curseforge_name"] == "The Real Mod Name"
    assert listed["name"] == "messy-filename-name"  # the local name is untouched
    assert detail["curseforge_name"] == "The Real Mod Name"


def test_get_mod_detail_includes_curseforge_url_when_linked(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Linked Mod", filename="linked.package")
    conn.execute(
        "UPDATE mods SET curseforge_id = 123, links = ? WHERE id = ?",
        ('{"curseforge_url": "https://www.curseforge.com/sims4/mods/linked-mod"}', mod_id),
    )
    conn.commit()

    body = client.get(f"/api/mods/{mod_id}").json()

    assert body["curseforge_url"] == "https://www.curseforge.com/sims4/mods/linked-mod"


def test_get_mod_detail_curseforge_url_is_none_when_unlinked(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Unlinked Mod", filename="unlinked.package")

    body = client.get(f"/api/mods/{mod_id}").json()

    assert body["curseforge_url"] is None


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


# --- translation detection + dependency confirm/reject ----------------------------
#
# Route tests stay thin: exercise one signal (name_heuristic, cheapest to
# trigger — no dbpf_writer fixture needed) to prove the wiring is correct.
# Every detection signal itself (description/name/STBL, weak vs strong) is
# already covered by tests/test_dependencies.py.


def test_detect_translation_finds_name_heuristic_signal(app_config, conn, tmp_path, client):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo", filename="source.package")
    candidate_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo [FR]", filename="candidate.package")

    response = client.post(f"/api/mods/{candidate_id}/detect-translation")

    assert response.status_code == 200
    body = response.json()
    assert any(s["source_mod_id"] == source_id and s["method"] == "name_heuristic" for s in body)
    assert body[0]["source_mod_name"] == "Better Woohoo"
    # Detection alone never writes anything — no dependency created yet.
    assert client.get(f"/api/mods/{candidate_id}").json()["dependencies"] == []


def test_detect_translation_unknown_mod_returns_404(client):
    response = client.post("/api/mods/does-not-exist/detect-translation")

    assert response.status_code == 404


def test_suggest_translation_creates_suggested_dependency(app_config, conn, tmp_path, client):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo", filename="source.package")
    candidate_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo [FR]", filename="candidate.package")

    response = client.post(
        f"/api/mods/{candidate_id}/suggest-translation", json={"source_mod_id": source_id}
    )

    assert response.status_code == 200
    dep = client.get(f"/api/mods/{candidate_id}").json()["dependencies"][0]
    assert dep["dependency_type"] == "translation"
    assert dep["confidence"] == "suggested"  # never lands as confirmed directly
    assert dep["resolved_name"] == "Better Woohoo"


def test_suggest_translation_unknown_source_mod_returns_404(app_config, conn, tmp_path, client):
    candidate_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo [FR]")

    response = client.post(
        f"/api/mods/{candidate_id}/suggest-translation", json={"source_mod_id": "does-not-exist"}
    )

    assert response.status_code == 404


def test_confirm_dependency_route_updates_confidence(app_config, conn, tmp_path, client):
    source_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    candidate_id = _install_mod(app_config, conn, tmp_path, "Needs Core", filename="needs.package")
    dependency_id = deps.suggest_translation(candidate_id, source_id, conn)

    response = client.post(f"/api/dependencies/{dependency_id}/confirm")

    assert response.status_code == 200
    dep = client.get(f"/api/mods/{candidate_id}").json()["dependencies"][0]
    assert dep["confidence"] == "confirmed"


def test_confirm_dependency_route_unknown_id_returns_404(client):
    response = client.post("/api/dependencies/999999/confirm")

    assert response.status_code == 404


def test_reject_dependency_route_deletes_it(app_config, conn, tmp_path, client):
    source_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    candidate_id = _install_mod(app_config, conn, tmp_path, "Needs Core", filename="needs.package")
    dependency_id = deps.suggest_translation(candidate_id, source_id, conn)

    response = client.post(f"/api/dependencies/{dependency_id}/reject")

    assert response.status_code == 200
    assert client.get(f"/api/mods/{candidate_id}").json()["dependencies"] == []


def test_reject_dependency_route_unknown_id_returns_404(client):
    response = client.post("/api/dependencies/999999/reject")

    assert response.status_code == 404


# --- CurseForge-declared dependencies ---------------------------------------------
#
# Route stays thin: the actual matching/idempotency logic is already covered
# in depth by tests/test_curseforge_dependencies.py — this only proves the
# route's own wiring (Direct Mode gate, 400/404/502 handling, request shape).


def test_detect_curseforge_dependencies_requires_direct_mode(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    conn.execute("UPDATE mods SET curseforge_id = ? WHERE id = ?", (100, mod_id))
    conn.commit()

    response = client.post(f"/api/mods/{mod_id}/detect-curseforge-dependencies")

    assert response.status_code == 400


def test_detect_curseforge_dependencies_unknown_mod_returns_404(app_config, tmp_path, monkeypatch):
    direct = _direct_client(app_config, tmp_path, monkeypatch, _FakeCurseForgeClient())

    response = direct.post("/api/mods/does-not-exist/detect-curseforge-dependencies")

    assert response.status_code == 404


def test_detect_curseforge_dependencies_unlinked_mod_returns_400(app_config, conn, tmp_path, monkeypatch):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")  # curseforge_id stays NULL
    direct = _direct_client(app_config, tmp_path, monkeypatch, _FakeCurseForgeClient())

    response = direct.post(f"/api/mods/{mod_id}/detect-curseforge-dependencies")

    assert response.status_code == 400


def test_detect_curseforge_dependencies_creates_suggested_row(app_config, conn, tmp_path, monkeypatch):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    core_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    conn.execute("UPDATE mods SET curseforge_id = ? WHERE id = ?", (100, mod_id))
    conn.execute("UPDATE mods SET curseforge_id = ? WHERE id = ?", (200, core_id))
    conn.commit()
    fake = _FakeCurseForgeClient(
        mod_by_id={100: _make_mod(mod_id=100, main_file_id=999)},
        file_by_key={
            (100, 999): curseforge.CurseForgeFile(
                file_id=999,
                file_name="mod-a.zip",
                download_url=None,
                game_version_min=None,
                game_version_max=None,
                release_type="release",
                dependencies=(curseforge.CurseForgeFileDependency(mod_id=200, relation_type=3),),
            )
        },
    )
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)

    response = direct.post(f"/api/mods/{mod_id}/detect-curseforge-dependencies")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["dependency_type"] == "required"
    assert body[0]["confidence"] == "suggested"
    assert body[0]["resolved_name"] == "Core Lib"


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


def test_set_namespace_override_route_updates_the_mod(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Some Mod")

    response = client.post(f"/api/mods/{mod_id}/namespace-override", json={"value": "Corrected"})

    assert response.status_code == 200
    assert response.json()["namespace_override"] == "Corrected"
    row = conn.execute("SELECT namespace_override FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["namespace_override"] == "Corrected"


def test_set_namespace_override_route_clears_with_null(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Some Mod")
    client.post(f"/api/mods/{mod_id}/namespace-override", json={"value": "Corrected"})

    response = client.post(f"/api/mods/{mod_id}/namespace-override", json={"value": None})

    assert response.status_code == 200
    assert response.json()["namespace_override"] is None


def test_set_namespace_override_route_unknown_mod_returns_404(client):
    response = client.post("/api/mods/does-not-exist/namespace-override", json={"value": "X"})

    assert response.status_code == 404


def test_get_mods_includes_namespace_override(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Some Mod")
    client.post(f"/api/mods/{mod_id}/namespace-override", json={"value": "Corrected"})

    body = client.get("/api/mods").json()

    assert body[0]["namespace_override"] == "Corrected"


def test_delete_mod_that_was_saved_creates_a_missing_mod_reminder(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Delete Me")
    client.post("/api/profiles", json={"name": "Weekend Build"})
    profile_id = client.get("/api/profiles").json()[0]["id"]
    client.put(f"/api/profiles/{profile_id}/mods", json={"mod_ids": [mod_id]})

    response = client.delete(f"/api/mods/{mod_id}")

    assert response.status_code == 200
    missing = client.get("/api/mods/missing").json()
    assert len(missing) == 1
    assert missing[0]["mod_id"] == mod_id
    assert missing[0]["name"] == "Delete Me"
    assert missing[0]["source_profile_names"] == "Weekend Build"


def test_delete_mod_not_in_any_saved_state_creates_no_reminder(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Delete Me")

    client.delete(f"/api/mods/{mod_id}")

    assert client.get("/api/mods/missing").json() == []


def test_dismiss_missing_mod_removes_it(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Delete Me")
    client.post("/api/profiles", json={"name": "Weekend Build"})
    profile_id = client.get("/api/profiles").json()[0]["id"]
    client.put(f"/api/profiles/{profile_id}/mods", json={"mod_ids": [mod_id]})
    client.delete(f"/api/mods/{mod_id}")
    entry_id = client.get("/api/mods/missing").json()[0]["id"]

    response = client.delete(f"/api/mods/missing/{entry_id}")

    assert response.status_code == 200
    assert client.get("/api/mods/missing").json() == []


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


# --- /api/catalog ------------------------------------------------------------------


class _FakeCurseForgeClient:
    """Stands in for curseforge.CurseForgeClient — nothing here touches the
    network or needs a real API key (see CLAUDE.md's testing note on
    mode-dependent code)."""

    def __init__(
        self,
        search_results=None,
        files_by_mod=None,
        mod_by_id=None,
        *,
        fail_search=None,
        fingerprint_matches=None,
        mods_by_id=None,
        fail_fingerprint_matches_with=None,
        file_by_key=None,
        get_mod_error_for=None,
    ):
        self._search_results = search_results or []
        self._files_by_mod = files_by_mod or {}
        self._mod_by_id = mod_by_id or {}
        self._fail_search = fail_search
        self._fingerprint_matches = fingerprint_matches or {}
        self._mods_by_id = mods_by_id or {}
        self._file_by_key = file_by_key or {}  # {(mod_id, file_id): CurseForgeFile} — see get_file()
        self._get_mod_error_for = get_mod_error_for or {}  # {curseforge_id: Exception} — see get_mod()
        # Raised once by match_fingerprints(), then cleared — enough to
        # simulate one transient failure for a retry test without needing a
        # call counter.
        self._fail_fingerprint_matches_with = fail_fingerprint_matches_with
        self.download_calls: list[tuple[int, int]] = []

    def verify_key(self) -> bool:
        return True

    def search_mods(self, query: str, *, game_version=None):
        if self._fail_search is not None:
            raise self._fail_search
        return self._search_results

    def get_files(self, mod_id: int):
        return self._files_by_mod.get(mod_id, [])

    def get_mod(self, mod_id: int):
        if mod_id in self._get_mod_error_for:
            raise self._get_mod_error_for[mod_id]
        return self._mod_by_id[mod_id]

    def get_file(self, mod_id: int, file_id: int):
        return self._file_by_key[(mod_id, file_id)]

    def download(self, mod_id: int, file_id: int, destination: Path) -> Path:
        self.download_calls.append((mod_id, file_id))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"data")
        return destination

    def match_fingerprints(self, fingerprints):
        if self._fail_fingerprint_matches_with is not None:
            exc, self._fail_fingerprint_matches_with = self._fail_fingerprint_matches_with, None
            raise exc
        return {fp: self._fingerprint_matches[fp] for fp in fingerprints if fp in self._fingerprint_matches}

    def get_mods(self, mod_ids):
        return [self._mods_by_id[i] for i in mod_ids if i in self._mods_by_id]


def _direct_client(app_config, tmp_path, monkeypatch, fake: _FakeCurseForgeClient) -> TestClient:
    monkeypatch.setattr("backend.main.curseforge.CurseForgeClient", lambda api_key: fake)
    direct_config = dataclasses.replace(app_config, curseforge_api_key="test-key")
    return TestClient(create_app(direct_config, db_path=tmp_path / "simslink.sqlite3"))


def _make_mod(**overrides) -> curseforge.CurseForgeMod:
    defaults = dict(
        mod_id=111,
        name="Better Woohoo",
        author="SomeAuthor",
        category="Gameplay",
        short_description="Makes it better.",
        thumbnail_url=None,
        curseforge_url="https://www.curseforge.com/sims4/mods/better-woohoo",
        third_party_distribution_allowed=True,
    )
    defaults.update(overrides)
    return curseforge.CurseForgeMod(**defaults)


def test_catalog_search_requires_direct_mode(client):
    response = client.get("/api/catalog/search", params={"q": "woohoo"})

    assert response.status_code == 400


def test_catalog_search_returns_results_in_direct_mode(app_config, tmp_path, monkeypatch):
    fake = _FakeCurseForgeClient(search_results=[_make_mod()])
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)

    response = direct.get("/api/catalog/search", params={"q": "woohoo"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "Better Woohoo"
    assert body[0]["third_party_distribution_allowed"] is True
    assert "compat_status" not in body[0]


def test_catalog_search_curseforge_error_returns_502(app_config, tmp_path, monkeypatch):
    fake = _FakeCurseForgeClient(fail_search=curseforge.CurseForgeError("rate limited"))
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)

    response = direct.get("/api/catalog/search", params={"q": "woohoo"})

    assert response.status_code == 502


def test_catalog_install_requires_direct_mode(client):
    response = client.post("/api/catalog/111/install")

    assert response.status_code == 400


def test_catalog_install_creates_mod_with_metadata(app_config, tmp_path, monkeypatch, conn):
    mod = _make_mod(third_party_distribution_allowed=True)
    fake = _FakeCurseForgeClient(
        files_by_mod={
            111: [
                curseforge.CurseForgeFile(
                    file_id=222,
                    file_name="better-woohoo.package",
                    download_url="https://example.com/222",
                    game_version_min="1.90",
                    game_version_max="1.110",
                    release_type="release",
                )
            ]
        },
        mod_by_id={111: mod},
    )
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)

    response = direct.post("/api/catalog/111/install")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Better Woohoo"
    assert fake.download_calls == [(111, 222)]
    row = conn.execute("SELECT * FROM mods WHERE curseforge_id = 111").fetchone()
    assert row is not None
    assert row["installed_version"] == "222"
    assert row["compat_status"] == "unknown"  # no game_version configured in app_config


def test_catalog_install_no_files_returns_502(app_config, tmp_path, monkeypatch):
    fake = _FakeCurseForgeClient(files_by_mod={}, mod_by_id={111: _make_mod()})
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)

    response = direct.post("/api/catalog/111/install")

    assert response.status_code == 502


# --- /api/open-external --------------------------------------------------------------


def test_open_external_opens_http_url(client, monkeypatch):
    calls = []
    monkeypatch.setattr("backend.main.webbrowser.open", lambda url: calls.append(url))

    response = client.post("/api/open-external", json={"url": "https://www.curseforge.com/x"})

    assert response.status_code == 200
    assert calls == ["https://www.curseforge.com/x"]


def test_open_external_rejects_non_http_scheme(client, monkeypatch):
    calls = []
    monkeypatch.setattr("backend.main.webbrowser.open", lambda url: calls.append(url))

    response = client.post("/api/open-external", json={"url": "file:///etc/passwd"})

    assert response.status_code == 400
    assert calls == []


# --- /api/updates --------------------------------------------------------------------


def test_updates_checklist_lists_only_mods_with_known_link(app_config, conn, tmp_path, client):
    linked_id = _install_mod(app_config, conn, tmp_path, "Linked Mod", filename="linked.package")
    conn.execute(
        "UPDATE mods SET links = ? WHERE id = ?",
        (json.dumps({"curseforge_url": "https://www.curseforge.com/x"}), linked_id),
    )
    conn.commit()
    _install_mod(app_config, conn, tmp_path, "Unlinked Mod", filename="unlinked.package")

    response = client.get("/api/updates/checklist")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == linked_id
    assert body[0]["curseforge_url"] == "https://www.curseforge.com/x"


def test_updates_check_requires_direct_mode(client):
    response = client.post("/api/updates/check")

    assert response.status_code == 400


def test_updates_check_reports_available_and_up_to_date(app_config, conn, tmp_path, monkeypatch):
    current_id = _install_mod(app_config, conn, tmp_path, "Current Mod", filename="current.package")
    outdated_id = _install_mod(app_config, conn, tmp_path, "Outdated Mod", filename="outdated.package")
    conn.execute("UPDATE mods SET curseforge_id = 111, installed_version = '222' WHERE id = ?", (current_id,))
    conn.execute("UPDATE mods SET curseforge_id = 333, installed_version = '444' WHERE id = ?", (outdated_id,))
    conn.commit()

    fake = _FakeCurseForgeClient(
        files_by_mod={
            111: [
                curseforge.CurseForgeFile(
                    file_id=222,
                    file_name="current.package",
                    download_url=None,
                    game_version_min=None,
                    game_version_max=None,
                    release_type="release",
                )
            ],
            333: [
                curseforge.CurseForgeFile(
                    file_id=555,
                    file_name="outdated-v2.package",
                    download_url=None,
                    game_version_min=None,
                    game_version_max=None,
                    release_type="release",
                )
            ],
        }
    )
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)

    response = direct.post("/api/updates/check")

    assert response.status_code == 200
    by_id = {entry["id"]: entry for entry in response.json()}
    assert by_id[current_id]["status"] == "up_to_date"
    assert by_id[outdated_id]["status"] == "update_available"
    assert by_id[outdated_id]["latest_file_id"] == 555


def test_updates_apply_installs_new_version(app_config, conn, tmp_path, monkeypatch):
    mod_id = _install_mod(app_config, conn, tmp_path, "Outdated Mod", filename="outdated.package")
    conn.execute("UPDATE mods SET curseforge_id = 111, installed_version = '222' WHERE id = ?", (mod_id,))
    conn.commit()

    mod_info = _make_mod(mod_id=111, name="Outdated Mod")
    fake = _FakeCurseForgeClient(
        files_by_mod={
            111: [
                curseforge.CurseForgeFile(
                    file_id=333,
                    file_name="outdated-v2.package",
                    download_url=None,
                    game_version_min="1.90",
                    game_version_max="1.120",
                    release_type="release",
                )
            ]
        },
        mod_by_id={111: mod_info},
    )
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)

    response = direct.post(f"/api/updates/{mod_id}/apply")

    assert response.status_code == 200
    assert fake.download_calls == [(111, 333)]
    row = conn.execute("SELECT installed_version FROM mods WHERE curseforge_id = 111").fetchone()
    assert row["installed_version"] == "333"


def test_updates_apply_requires_curseforge_link(app_config, conn, tmp_path, monkeypatch):
    mod_id = _install_mod(app_config, conn, tmp_path, "No Link Mod")
    fake = _FakeCurseForgeClient()
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)

    response = direct.post(f"/api/updates/{mod_id}/apply")

    assert response.status_code == 400


# --- /api/crash --------------------------------------------------------------------


def test_analyze_crash_reports_not_found_without_exception_file(client):
    response = client.post("/api/crash/analyze")

    assert response.status_code == 200
    assert response.json() == {"found": False, "reports": []}


def test_analyze_crash_finds_direct_trace_suspect(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "bettermod", filename="bettermod.ts4script")
    (app_config.sims4_user_dir / "lastException.txt").write_text(
        (FIXTURES / "lastexception_mod_in_trace.txt").read_text()
    )

    response = client.post("/api/crash/analyze")

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert len(body["reports"]) == 1
    report = body["reports"][0]
    assert report["crash_log_id"] is not None
    assert any(s["mod_id"] == mod_id for s in report["suspects"])


def test_analyze_crash_splits_real_xml_file_into_one_report_per_occurrence(app_config, conn, client):
    (app_config.sims4_user_dir / "lastException.txt").write_text(
        (FIXTURES / "lastexception_real_multi_report.txt").read_text()
    )

    response = client.post("/api/crash/analyze")

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert len(body["reports"]) == 3
    assert len({r["crash_log_id"] for r in body["reports"]}) == 3


def test_bisection_full_roundtrip_confirms_faulty_mod(app_config, conn, tmp_path, client):
    mod_ids = [
        _install_mod(app_config, conn, tmp_path, f"Mod{i}", filename=f"mod{i}.package") for i in range(4)
    ]
    culprit = mod_ids[3]
    (app_config.sims4_user_dir / "lastException.txt").write_text(
        (FIXTURES / "lastexception_core_only.txt").read_text()
    )

    analyze_resp = client.post("/api/crash/analyze")
    report = analyze_resp.json()["reports"][0]
    crash_log_id = report["crash_log_id"]
    assert report["suspects"] == []

    start_resp = client.post(f"/api/crash/{crash_log_id}/bisection/start")
    assert start_resp.status_code == 200

    status = "next_round"
    disabled = start_resp.json()["disabled"]
    for _ in range(6):
        if status != "next_round":
            break
        crash_occurred = culprit not in disabled
        report_resp = client.post(
            f"/api/crash/{crash_log_id}/bisection/report", json={"crash_occurred": crash_occurred}
        )
        assert report_resp.status_code == 200
        body = report_resp.json()
        status = body["status"]
        disabled = body.get("disabled", [])

    assert status == "converged"
    converged_mod_id = report_resp.json()["mod_id"]
    assert converged_mod_id == culprit

    confirm_resp = client.post(
        f"/api/crash/{crash_log_id}/confirm-faulty", json={"mod_id": converged_mod_id}
    )
    assert confirm_resp.status_code == 200
    row = conn.execute(
        "SELECT confirmed_faulty_mod_id FROM crash_log WHERE id = ?", (crash_log_id,)
    ).fetchone()
    assert row["confirmed_faulty_mod_id"] == culprit
    # Still not deleted — confirmation only records, never destroys.
    assert conn.execute("SELECT COUNT(*) FROM mods WHERE id = ?", (culprit,)).fetchone()[0] == 1


def test_bisection_start_unknown_crash_log_returns_400(client):
    response = client.post("/api/crash/999/bisection/start")

    assert response.status_code == 400


# --- /api/cache --------------------------------------------------------------------


def test_cache_targets_and_clean_roundtrip(app_config, client):
    (app_config.sims4_user_dir / "localthumbcache.package").write_bytes(b"x")
    (app_config.sims4_user_dir / "options.ini").write_bytes(b"protected")

    targets_resp = client.get("/api/cache/targets")
    assert targets_resp.status_code == 200
    names = [t["name"] for t in targets_resp.json()]
    assert "localthumbcache.package" in names

    clean_resp = client.post("/api/cache/clean")
    assert clean_resp.status_code == 200
    assert "localthumbcache.package" in clean_resp.json()["cleaned"]
    assert not (app_config.sims4_user_dir / "localthumbcache.package").exists()
    assert (app_config.sims4_user_dir / "options.ini").exists()


def test_open_cache_target_file_opens_its_parent_dir(app_config, client, monkeypatch):
    (app_config.sims4_user_dir / "localthumbcache.package").write_bytes(b"x")
    calls = []
    monkeypatch.setattr("backend.main.subprocess.Popen", lambda args: calls.append(args))

    response = client.post("/api/cache/targets/localthumbcache.package/open")

    assert response.status_code == 200
    assert calls == [["xdg-open", str(app_config.sims4_user_dir)]]


def test_open_cache_target_directory_opens_itself(app_config, client, monkeypatch):
    (app_config.sims4_user_dir / "cache").mkdir()
    calls = []
    monkeypatch.setattr("backend.main.subprocess.Popen", lambda args: calls.append(args))

    response = client.post("/api/cache/targets/cache/open")

    assert response.status_code == 200
    assert calls == [["xdg-open", str(app_config.sims4_user_dir / "cache")]]


def test_open_cache_target_unknown_name_returns_404(client):
    response = client.post("/api/cache/targets/not-a-real-target/open")

    assert response.status_code == 404


# --- /api/settings --------------------------------------------------------------------


def test_open_mods_folder_invokes_xdg_open(app_config, client, monkeypatch):
    calls = []
    monkeypatch.setattr("backend.main.subprocess.Popen", lambda args: calls.append(args))

    response = client.post("/api/settings/open-mods-folder")

    assert response.status_code == 200
    assert calls == [["xdg-open", str(app_config.sims4_mods_dir)]]


def test_pick_folder_returns_chosen_path(client, monkeypatch, tmp_path):
    chosen = tmp_path / "Chosen Folder"
    captured_args = []

    class FakeResult:
        returncode = 0
        stdout = f"{chosen}\n"

    def fake_run(args, **kwargs):
        captured_args.extend(args)
        return FakeResult()

    monkeypatch.setattr("backend.main.subprocess.run", fake_run)

    response = client.post("/api/settings/pick-folder", json={"initial_dir": str(tmp_path)})

    assert response.status_code == 200
    assert response.json() == {"path": str(chosen)}
    assert captured_args[:3] == ["zenity", "--file-selection", "--directory"]
    assert f"--filename={tmp_path}/" in captured_args


def test_pick_folder_returns_none_when_cancelled(client, monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr("backend.main.subprocess.run", lambda args, **kwargs: FakeResult())

    response = client.post("/api/settings/pick-folder", json={})

    assert response.status_code == 200
    assert response.json() == {"path": None}


def test_pick_folder_unavailable_returns_501(client, monkeypatch):
    def fake_run(args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr("backend.main.subprocess.run", fake_run)

    response = client.post("/api/settings/pick-folder", json={})

    assert response.status_code == 501


def test_get_settings_returns_configured_paths(app_config, client):
    response = client.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["game_dir"] == str(app_config.sims4_game_dir)
    assert body["mods_dir"] == str(app_config.sims4_mods_dir)
    assert body["library_dir"] == str(app_config.library_dir)
    assert body["backup_retention_count"] == app_config.backup_retention_count
    assert body["mods_watcher_enabled"] == app_config.mods_watcher_enabled
    assert body["log_level"] == app_config.log_level


def test_update_paths_route_persists_and_returns_new_values(app_config, tmp_path, client):
    new_library = tmp_path / "new-library"

    response = client.post("/api/settings/paths", json={"library_dir": str(new_library)})

    assert response.status_code == 200
    body = response.json()
    assert body["library_dir"] == str(new_library)
    assert body["restart_required"] is True

    follow_up = client.get("/api/settings")
    assert follow_up.json()["library_dir"] == str(new_library)


def test_update_paths_route_migrates_installed_mods(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    new_library = tmp_path / "new-library"

    response = client.post("/api/settings/paths", json={"library_dir": str(new_library)})

    assert response.status_code == 200
    assert (new_library / mod_id / "mymod.package").is_file()
    row = conn.execute("SELECT library_path FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["library_path"] == str(new_library / mod_id)


def test_update_paths_route_rejects_incoherent_combination(app_config, client):
    bad_library = app_config.sims4_mods_dir / "nested"

    response = client.post("/api/settings/paths", json={"library_dir": str(bad_library)})

    assert response.status_code == 400


def test_update_paths_route_warns_on_unrecognizable_game_dir(app_config, tmp_path, client):
    new_game_dir = tmp_path / "some-other-folder"
    new_game_dir.mkdir()

    response = client.post("/api/settings/paths", json={"sims4_game_dir": str(new_game_dir)})

    assert response.status_code == 200
    assert len(response.json()["warnings"]) == 1


def test_full_scan_rehashes_and_reports_stats(app_config, conn, tmp_path, client):
    mod_id = mod_manager.install(
        _write_zip(tmp_path / "source.zip", "mymod.package"),
        config=app_config, conn=conn, mod_name="Cool Mod",
    )
    old_hash = conn.execute(
        "SELECT hash FROM mod_files WHERE mod_id = ?", (mod_id,)
    ).fetchone()["hash"]
    (Path(app_config.library_dir) / mod_id / "mymod.package").write_bytes(b"changed-data")

    response = client.post("/api/settings/full-scan")

    assert response.status_code == 200
    body = response.json()
    assert body["mods_scanned"] == 1
    assert body["mods_imported"] == 0
    assert body["files_hashed"] == 1
    new_hash = conn.execute(
        "SELECT hash FROM mod_files WHERE mod_id = ?", (mod_id,)
    ).fetchone()["hash"]
    assert new_hash != old_hash


def test_full_scan_also_imports_untracked_mods(app_config, conn, client):
    # Regression: a real folder dropped directly under Mods/ (e.g. a
    # duplicated mod folder from outside the app) used to stay invisible
    # to "Full scan" — it only ever re-hashed mods already in the DB.
    unmanaged = app_config.sims4_mods_dir / "duplicated-mod"
    unmanaged.mkdir(parents=True)
    (unmanaged / "dup.package").write_bytes(b"data")

    response = client.post("/api/settings/full-scan")

    assert response.status_code == 200
    body = response.json()
    assert body["mods_imported"] == 1
    assert body["mods_scanned"] == 1
    row = conn.execute("SELECT id FROM mods WHERE id = 'duplicated-mod'").fetchone()
    assert row is not None
    assert (app_config.sims4_mods_dir / "duplicated-mod").is_symlink()


# --- Loose mods (files dropped directly at Mods/ root) --------------------------------


def test_import_loose_files_route_adopts_and_tags_a_loose_package(app_config, conn, client):
    (app_config.sims4_mods_dir / "SomeMod.package").write_bytes(b"data")

    response = client.post("/api/settings/import-loose-files")

    assert response.status_code == 200
    assert response.json() == {"mods_imported": 1}
    row = conn.execute("SELECT is_loose_import FROM mods WHERE name = 'SomeMod'").fetchone()
    assert row["is_loose_import"] == 1


def test_get_mods_reports_is_loose_import_flag(app_config, conn, client):
    (app_config.sims4_mods_dir / "SomeMod.package").write_bytes(b"data")
    client.post("/api/settings/import-loose-files")

    body = client.get("/api/mods").json()

    assert body[0]["is_loose_import"] is True


def test_match_curseforge_start_requires_direct_mode(client):
    response = client.post("/api/settings/match-curseforge/start")

    assert response.status_code == 400


def test_match_curseforge_step_without_a_session_returns_400(app_config, tmp_path, monkeypatch):
    fake = _FakeCurseForgeClient()
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)

    response = direct.post("/api/settings/match-curseforge/step")

    assert response.status_code == 400


def test_match_curseforge_start_reports_the_candidate_count(app_config, tmp_path, monkeypatch):
    (app_config.sims4_mods_dir / "SomeMod.package").write_bytes(b"real content")
    fake = _FakeCurseForgeClient()
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)
    direct.post("/api/settings/import-loose-files")

    response = direct.post("/api/settings/match-curseforge/start")

    assert response.status_code == 200
    assert response.json() == {"total": 1, "checked": 0, "matched": 0, "skipped_too_large": 0, "done": False}


def test_match_curseforge_step_links_a_matched_loose_mod_and_reports_done(app_config, tmp_path, monkeypatch):
    (app_config.sims4_mods_dir / "SomeMod.package").write_bytes(b"real content")
    fingerprint = curseforge.curseforge_fingerprint(b"real content")
    fake = _FakeCurseForgeClient(
        fingerprint_matches={fingerprint: 111},
        mods_by_id={111: _make_mod(mod_id=111, author="RealAuthor", thumbnail_url="https://x/y.png")},
    )
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)
    direct.post("/api/settings/import-loose-files")
    direct.post("/api/settings/match-curseforge/start")

    response = direct.post("/api/settings/match-curseforge/step")

    assert response.status_code == 200
    assert response.json() == {"total": 1, "checked": 1, "matched": 1, "skipped_too_large": 0, "done": True}
    mod = direct.get("/api/mods").json()[0]
    assert mod["curseforge_id"] == 111

    # A step call after the session finished (and was cleared) is the same
    # "nothing in progress" state as never having started one.
    after_done = direct.post("/api/settings/match-curseforge/step")
    assert after_done.status_code == 400
    assert mod["author"] == "RealAuthor"


def test_match_curseforge_step_returns_502_and_preserves_progress_on_transient_failure(
    app_config, tmp_path, monkeypatch
):
    (app_config.sims4_mods_dir / "SomeMod.package").write_bytes(b"real content")
    fingerprint = curseforge.curseforge_fingerprint(b"real content")
    fake = _FakeCurseForgeClient(
        fingerprint_matches={fingerprint: 111},
        mods_by_id={111: _make_mod(mod_id=111, author="RealAuthor")},
        fail_fingerprint_matches_with=curseforge.CurseForgeError("rate limited"),
    )
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)
    direct.post("/api/settings/import-loose-files")
    direct.post("/api/settings/match-curseforge/start")

    failed = direct.post("/api/settings/match-curseforge/step")
    assert failed.status_code == 502
    mod = direct.get("/api/mods").json()[0]
    assert mod["curseforge_id"] is None  # nothing committed from the failed attempt

    # The one-shot failure is cleared now — retrying the exact same call
    # (session untouched by run_step() on failure) succeeds.
    retried = direct.post("/api/settings/match-curseforge/step")
    assert retried.status_code == 200
    assert retried.json() == {"total": 1, "checked": 1, "matched": 1, "skipped_too_large": 0, "done": True}
    mod = direct.get("/api/mods").json()[0]
    assert mod["curseforge_id"] == 111


def test_match_curseforge_step_auth_failure_clears_the_session(app_config, tmp_path, monkeypatch):
    (app_config.sims4_mods_dir / "SomeMod.package").write_bytes(b"real content")
    fake = _FakeCurseForgeClient(fail_fingerprint_matches_with=curseforge.CurseForgeAuthError("key rejected"))
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)
    direct.post("/api/settings/import-loose-files")
    direct.post("/api/settings/match-curseforge/start")

    response = direct.post("/api/settings/match-curseforge/step")

    assert response.status_code == 401
    # Not retryable — the session is gone, same "nothing in progress" state
    # a never-started or already-finished run would be in.
    after = direct.post("/api/settings/match-curseforge/step")
    assert after.status_code == 400


def test_sync_curseforge_start_requires_direct_mode(client):
    response = client.post("/api/settings/sync-curseforge/start")

    assert response.status_code == 400


def test_sync_curseforge_step_without_a_session_returns_400(app_config, tmp_path, monkeypatch):
    fake = _FakeCurseForgeClient()
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)

    response = direct.post("/api/settings/sync-curseforge/step")

    assert response.status_code == 400


def test_sync_curseforge_start_reports_linked_mod_count(app_config, conn, tmp_path, monkeypatch):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    conn.execute("UPDATE mods SET curseforge_id = ? WHERE id = ?", (111, mod_id))
    conn.commit()
    fake = _FakeCurseForgeClient()
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)

    response = direct.post("/api/settings/sync-curseforge/start")

    assert response.status_code == 200
    assert response.json() == {"total": 1, "checked": 0, "synced": 0, "errors": 0, "done": False}


def test_sync_curseforge_step_updates_compat_status_and_reports_done(app_config, conn, tmp_path, monkeypatch):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    conn.execute("UPDATE mods SET curseforge_id = ? WHERE id = ?", (111, mod_id))
    conn.commit()
    fake = _FakeCurseForgeClient(
        mod_by_id={111: _make_mod(mod_id=111, main_file_id=222)},
        file_by_key={
            (111, 222): curseforge.CurseForgeFile(
                file_id=222,
                file_name="mod-a.zip",
                download_url=None,
                game_version_min="1.90",
                game_version_max="1.100",
                release_type="release",
            )
        },
    )
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)
    direct.post("/api/settings/sync-curseforge/start")

    response = direct.post("/api/settings/sync-curseforge/step")

    assert response.status_code == 200
    assert response.json() == {"total": 1, "checked": 1, "synced": 1, "errors": 0, "done": True}
    mod = direct.get(f"/api/mods/{mod_id}").json()
    # app_config's game_version is None by default — compat_status() can't
    # classify without it (see test_curseforge_dependencies.py's dedicated
    # "game_version given" case for the classification itself), but
    # game_version_min/max (not part of the API response — checked via the
    # DB directly) are still stored either way.
    assert mod["compat_status"] == "unknown"
    row = conn.execute(
        "SELECT game_version_min, game_version_max FROM mods WHERE id = ?", (mod_id,)
    ).fetchone()
    assert row["game_version_min"] == "1.90"
    assert row["game_version_max"] == "1.100"


def test_sync_curseforge_step_auth_failure_clears_the_session(app_config, conn, tmp_path, monkeypatch):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    conn.execute("UPDATE mods SET curseforge_id = ? WHERE id = ?", (111, mod_id))
    conn.commit()
    fake = _FakeCurseForgeClient(get_mod_error_for={111: curseforge.CurseForgeAuthError("key rejected")})
    direct = _direct_client(app_config, tmp_path, monkeypatch, fake)
    direct.post("/api/settings/sync-curseforge/start")

    response = direct.post("/api/settings/sync-curseforge/step")

    assert response.status_code == 401
    after = direct.post("/api/settings/sync-curseforge/step")
    assert after.status_code == 400


# --- Compat quarantine ---------------------------------------------------------


def test_compat_quarantine_preview_reports_incompatible_active_mods(app_config, conn, client, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    conn.execute("UPDATE mods SET compat_status = 'incompatible' WHERE id = ?", (mod_id,))
    conn.commit()

    response = client.get("/api/compat/quarantine/preview")

    assert response.status_code == 200
    assert response.json() == [{"mod_id": mod_id, "name": "Mod A", "reason": "incompatible"}]


def test_compat_quarantine_route_disables_only_the_confirmed_subset(app_config, conn, client, tmp_path):
    keep_id = _install_mod(app_config, conn, tmp_path, "Mod Keep")
    drop_id = _install_mod(app_config, conn, tmp_path, "Mod Drop")
    conn.execute("UPDATE mods SET compat_status = 'incompatible' WHERE id IN (?, ?)", (keep_id, drop_id))
    conn.commit()

    # Only confirm drop_id — keep_id must stay active even though it also
    # showed up in the preview, since the route never trusts more than the
    # intersection of the request body with a freshly recomputed preview.
    response = client.post("/api/compat/quarantine", json={"mod_ids": [drop_id]})

    assert response.status_code == 200
    assert response.json() == {"quarantined": [drop_id]}
    assert conn.execute("SELECT active FROM mods WHERE id = ?", (drop_id,)).fetchone()["active"] == 0
    assert conn.execute("SELECT active FROM mods WHERE id = ?", (keep_id,)).fetchone()["active"] == 1


def test_compat_quarantine_route_ignores_unrelated_mod_id_in_payload(app_config, conn, client, tmp_path):
    other_id = _install_mod(app_config, conn, tmp_path, "Unrelated Compatible Mod")

    response = client.post("/api/compat/quarantine", json={"mod_ids": [other_id]})

    assert response.status_code == 200
    assert response.json() == {"quarantined": []}
    assert conn.execute("SELECT active FROM mods WHERE id = ?", (other_id,)).fetchone()["active"] == 1


def test_compat_quarantine_list_and_release_round_trip(app_config, conn, client, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    conn.execute("UPDATE mods SET compat_status = 'incompatible' WHERE id = ?", (mod_id,))
    conn.commit()
    client.post("/api/compat/quarantine", json={"mod_ids": [mod_id]})

    listed = client.get("/api/compat/quarantine")
    assert listed.status_code == 200
    assert [row["mod_id"] for row in listed.json()] == [mod_id]

    # Not ready yet — still marked incompatible.
    release = client.post("/api/compat/quarantine/release")
    assert release.json() == {"released": [], "still_incompatible": [mod_id], "failed": []}

    conn.execute("UPDATE mods SET compat_status = 'compatible' WHERE id = ?", (mod_id,))
    conn.commit()
    release = client.post("/api/compat/quarantine/release")
    assert release.json() == {"released": [mod_id], "still_incompatible": [], "failed": []}
    assert conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()["active"] == 1
    assert client.get("/api/compat/quarantine").json() == []


def test_compat_quarantine_forget_route_stops_tracking(app_config, conn, client, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    conn.execute("UPDATE mods SET compat_status = 'incompatible' WHERE id = ?", (mod_id,))
    conn.commit()
    client.post("/api/compat/quarantine", json={"mod_ids": [mod_id]})

    response = client.delete(f"/api/compat/quarantine/{mod_id}")

    assert response.status_code == 200
    assert client.get("/api/compat/quarantine").json() == []
    # Forgetting is bookkeeping-only — the mod stays disabled either way.
    assert conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()["active"] == 0


def test_suggested_groups_route_clusters_related_loose_mods(app_config, conn, client):
    for suffix in ("ItemA", "ItemB", "ItemC"):
        (app_config.sims4_mods_dir / f"creator_pack_{suffix}.package").write_bytes(b"data")
    client.post("/api/settings/import-loose-files")

    response = client.get("/api/mods/loose/suggested-groups")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert len(body[0]["mod_ids"]) == 3
    assert body[0]["curseforge_id"] is None


def test_suggested_groups_route_reports_confirmed_curseforge_groups(app_config, conn, client):
    for name in ("TotallyUnrelatedNameOne", "CompletelyDifferentNameTwo"):
        (app_config.sims4_mods_dir / f"{name}.package").write_bytes(b"data")
    client.post("/api/settings/import-loose-files")
    conn.execute("UPDATE mods SET curseforge_id = 12345")
    conn.commit()

    body = client.get("/api/mods/loose/suggested-groups").json()

    assert len(body) == 1
    assert body[0]["curseforge_id"] == 12345
    assert len(body[0]["mod_ids"]) == 2


def test_merge_loose_mods_route_combines_them(app_config, conn, client):
    for suffix in ("ItemA", "ItemB"):
        (app_config.sims4_mods_dir / f"creator_pack_{suffix}.package").write_bytes(b"data")
    client.post("/api/settings/import-loose-files")
    mod_ids = [m["mod_ids"] for m in client.get("/api/mods/loose/suggested-groups").json()][0]

    response = client.post("/api/mods/loose/merge", json={"mod_ids": mod_ids, "new_name": "Creator Pack"})

    assert response.status_code == 200
    new_mod_id = response.json()["mod_id"]
    assert conn.execute("SELECT name FROM mods WHERE id = ?", (new_mod_id,)).fetchone()["name"] == "Creator Pack"
    for old_id in mod_ids:
        assert conn.execute("SELECT 1 FROM mods WHERE id = ?", (old_id,)).fetchone() is None


def test_merge_loose_mods_route_rejects_a_single_mod(app_config, conn, client):
    (app_config.sims4_mods_dir / "SomeMod.package").write_bytes(b"data")
    client.post("/api/settings/import-loose-files")
    mod_id = conn.execute("SELECT id FROM mods").fetchone()["id"]

    response = client.post("/api/mods/loose/merge", json={"mod_ids": [mod_id], "new_name": "Solo"})

    assert response.status_code == 400


# --- /api/downloads (Assisted Mode detection) -----------------------------------------
#
# app.state.report_download(path) simulates a DownloadWatcher detection
# synchronously, with no real filesystem watcher thread involved — the
# watcher itself (watchdog Observer + debounce timers) is exercised for real
# in tests/test_download_watcher.py; here we only need the app-level wiring
# (pending-download store + routes) to be correct.


def _write_zip(path: Path, filename: str, content: bytes = b"data") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(filename, content)
    return path


def test_pending_downloads_empty_initially(client):
    response = client.get("/api/downloads/pending")

    assert response.status_code == 200
    assert response.json() == []


def test_report_download_adds_to_pending_list_without_candidate(app, client, tmp_path):
    archive = _write_zip(tmp_path / "NewMod.zip", "new.package")

    app.state.report_download(archive)

    items = client.get("/api/downloads/pending").json()
    assert len(items) == 1
    assert items[0]["filename"] == "NewMod.zip"
    assert items[0]["candidate_mod_id"] is None
    assert "path" not in items[0]  # never leak the raw filesystem path


def test_report_download_suggests_replace_candidate_for_similar_name(
    app, client, app_config, conn, tmp_path
):
    mod_id = mod_manager.install(
        _write_zip(tmp_path / "source.zip", "mymod.package"),
        config=app_config, conn=conn, mod_name="Cool Mod",
    )
    archive = _write_zip(tmp_path / "Cool-Mod-v2.zip", "mymod.package")

    app.state.report_download(archive)

    items = client.get("/api/downloads/pending").json()
    assert items[0]["candidate_mod_id"] == mod_id
    assert items[0]["candidate_mod_name"] == "Cool Mod"


def test_install_pending_download_installs_new_mod(app, client, tmp_path):
    archive = _write_zip(tmp_path / "NewMod.zip", "new.package")
    app.state.report_download(archive)
    token = client.get("/api/downloads/pending").json()[0]["token"]

    response = client.post(f"/api/downloads/{token}/install")

    assert response.status_code == 200
    assert response.json()["name"] == "NewMod"
    assert client.get("/api/downloads/pending").json() == []  # consumed


def test_install_pending_download_unknown_token_returns_404(client):
    response = client.post("/api/downloads/does-not-exist/install")

    assert response.status_code == 404


def test_replace_pending_download_replaces_existing_mod(app, client, app_config, conn, tmp_path):
    mod_id = mod_manager.install(
        _write_zip(tmp_path / "source.zip", "old.package"),
        config=app_config, conn=conn, mod_name="Cool Mod",
    )
    archive = _write_zip(tmp_path / "CoolModV2.zip", "new.package")
    app.state.report_download(archive)
    token = client.get("/api/downloads/pending").json()[0]["token"]

    response = client.post(f"/api/downloads/{token}/replace", json={"mod_id": mod_id})

    assert response.status_code == 200
    row = conn.execute("SELECT * FROM mods WHERE id = ?", (response.json()["id"],)).fetchone()
    assert (Path(row["library_path"]) / "new.package").is_file()
    assert not (Path(row["library_path"]) / "old.package").exists()


def test_dismiss_pending_download_removes_without_installing(app, client, tmp_path):
    archive = _write_zip(tmp_path / "NewMod.zip", "new.package")
    app.state.report_download(archive)
    token = client.get("/api/downloads/pending").json()[0]["token"]

    response = client.post(f"/api/downloads/{token}/dismiss")

    assert response.status_code == 200
    assert client.get("/api/downloads/pending").json() == []
    assert client.get("/api/mods").json() == []  # never installed


# --- Mods/ real-time watcher + startup scan --------------------------------------------
#
# The real watchdog Observer (scanner.ModsFolderWatcher) is exercised for
# real in tests/test_scanner.py; here we only check backend/main.py's own
# wiring: that it's built-but-not-started, and that the debounce/startup-scan
# functions it exposes on app.state actually call into scanner.py correctly.


def test_mods_watcher_is_built_but_not_auto_started(app):
    assert isinstance(app.state.mods_watcher, scanner.ModsFolderWatcher)


def test_run_startup_scan_imports_untracked_mods_into_db(app, app_config, conn):
    # A real directory dropped directly under Mods/ before SimsLink managed
    # it — exactly what import_untracked_mods() is for.
    unmanaged = app_config.sims4_mods_dir / "some-preexisting-mod"
    unmanaged.mkdir(parents=True)
    (unmanaged / "preexisting.package").write_bytes(b"data")

    app.state.run_startup_scan()

    row = conn.execute("SELECT id FROM mods WHERE id = 'some-preexisting-mod'").fetchone()
    assert row is not None
    assert (app_config.sims4_mods_dir / "some-preexisting-mod").is_symlink()


def test_schedule_mods_rescan_debounces_then_reruns_incremental_scan(app, app_config, conn, tmp_path):
    mod_id = mod_manager.install(
        _write_zip(tmp_path / "source.zip", "mymod.package"),
        config=app_config, conn=conn, mod_name="Cool Mod",
    )
    old_hash = conn.execute(
        "SELECT hash FROM mod_files WHERE mod_id = ?", (mod_id,)
    ).fetchone()["hash"]

    time.sleep(0.05)  # ensure a distinct mtime from the install above
    (Path(app_config.library_dir) / mod_id / "mymod.package").write_bytes(b"changed-data")

    app.state.schedule_mods_rescan()
    app.state.schedule_mods_rescan()  # a second event inside the debounce window: still one scan

    time.sleep(2.5)  # past the 2s debounce in backend/main.py

    new_hash = conn.execute(
        "SELECT hash FROM mod_files WHERE mod_id = ?", (mod_id,)
    ).fetchone()["hash"]
    assert new_hash != old_hash


def test_regression_schedule_mods_rescan_also_imports_untracked_mods(app, app_config, conn):
    # A real folder dropped directly under Mods/ while the app is already
    # running (e.g. a duplicated mod folder) used to stay invisible until
    # the next full app restart — the watcher fired, but its debounced
    # rescan only ever re-hashed mods already in the DB, never adopted new
    # ones the way the one-time startup scan did.
    unmanaged = app_config.sims4_mods_dir / "duplicated-mod"
    unmanaged.mkdir(parents=True)
    (unmanaged / "dup.package").write_bytes(b"data")

    app.state.schedule_mods_rescan()
    time.sleep(2.5)  # past the 2s debounce in backend/main.py

    row = conn.execute("SELECT id FROM mods WHERE id = 'duplicated-mod'").fetchone()
    assert row is not None
    assert (app_config.sims4_mods_dir / "duplicated-mod").is_symlink()


# --- /api/profiles --------------------------------------------------------------------


def test_create_and_list_profile(client):
    create_resp = client.post("/api/profiles", json={"name": "Build Only"})
    assert create_resp.status_code == 200

    list_resp = client.get("/api/profiles")
    body = list_resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "Build Only"
    assert body[0]["mod_ids"] == []
    assert body[0]["created_date"]


def test_create_profile_duplicate_name_returns_400(client):
    client.post("/api/profiles", json={"name": "Build Only"})

    response = client.post("/api/profiles", json={"name": "Build Only"})

    assert response.status_code == 400


def test_set_profile_mods_updates_membership(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    profile_id = client.post("/api/profiles", json={"name": "Build Only"}).json()["id"]

    response = client.put(f"/api/profiles/{profile_id}/mods", json={"mod_ids": [mod_id]})

    assert response.status_code == 200
    assert response.json()["mod_ids"] == [mod_id]


def test_set_profile_mods_unknown_profile_returns_404(client):
    response = client.put("/api/profiles/999999/mods", json={"mod_ids": []})

    assert response.status_code == 404


def test_activate_profile_switches_active_mods(app_config, conn, tmp_path, client):
    # Additive-only (see profiles.activate_profile()'s docstring): mod_a was
    # disabled since the save, so loading it re-enables mod_a — mod_b, which
    # isn't part of this saved state at all, is left untouched rather than
    # being disabled.
    mod_a = _install_mod(app_config, conn, tmp_path, "Mod A", filename="a.package")
    mod_b = _install_mod(app_config, conn, tmp_path, "Mod B", filename="b.package")
    profile_id = client.post("/api/profiles", json={"name": "A Only"}).json()["id"]
    client.put(f"/api/profiles/{profile_id}/mods", json={"mod_ids": [mod_a]})
    client.post(f"/api/mods/{mod_a}/disable")

    response = client.post(f"/api/profiles/{profile_id}/activate")

    assert response.status_code == 200
    mods = {m["id"]: m["active"] for m in client.get("/api/mods").json()}
    assert mods[mod_a] is True
    assert mods[mod_b] is True


def test_activate_profile_unknown_returns_404(client):
    response = client.post("/api/profiles/999999/activate")

    assert response.status_code == 404


def test_delete_profile_removes_it(client):
    profile_id = client.post("/api/profiles", json={"name": "Build Only"}).json()["id"]

    response = client.delete(f"/api/profiles/{profile_id}")

    assert response.status_code == 200
    assert client.get("/api/profiles").json() == []


# --- /api/blacklist --------------------------------------------------------------------


def test_add_and_list_blacklist_entry(client):
    response = client.post("/api/blacklist", json={"pattern": "badmod", "note": "corrupts saves"})

    assert response.status_code == 200
    body = client.get("/api/blacklist").json()
    assert len(body) == 1
    assert body[0]["pattern"] == "badmod"
    assert body[0]["note"] == "corrupts saves"


def test_add_blacklist_entry_rejects_empty_pattern(client):
    response = client.post("/api/blacklist", json={"pattern": "   "})

    assert response.status_code == 400


def test_remove_blacklist_entry(client):
    entry_id = client.post("/api/blacklist", json={"pattern": "badmod"}).json()["id"]

    response = client.delete(f"/api/blacklist/{entry_id}")

    assert response.status_code == 200
    assert client.get("/api/blacklist").json() == []


def test_blacklist_matches_flags_installed_mod(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Totally BadMod Deluxe")
    client.post("/api/blacklist", json={"pattern": "badmod"})

    response = client.get("/api/blacklist/matches")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["mod_id"] == mod_id
    assert body[0]["patterns"] == ["badmod"]
    assert len(body[0]["pattern_ids"]) == 1


def test_regression_disabled_mod_excluded_from_blacklist_matches(app_config, conn, tmp_path, client):
    mod_id = _install_mod(app_config, conn, tmp_path, "Totally BadMod Deluxe")
    client.post("/api/blacklist", json={"pattern": "badmod"})
    client.post(f"/api/mods/{mod_id}/disable")

    response = client.get("/api/blacklist/matches")

    assert response.json() == []


def test_blacklist_matches_empty_when_no_entries(app_config, conn, tmp_path, client):
    _install_mod(app_config, conn, tmp_path, "Any Mod")

    response = client.get("/api/blacklist/matches")

    assert response.json() == []
