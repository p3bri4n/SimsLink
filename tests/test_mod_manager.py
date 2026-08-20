import zipfile
from pathlib import Path

import pytest

import mod_manager


def make_zip(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        for rel_path, content in files.items():
            zf.writestr(rel_path, content)
    return zip_path


# --- .ts4script depth rule (CLAUDE.md priority coverage) ------------------


def test_install_flattens_nested_ts4script_to_mod_root(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"Scripts/mymod.ts4script": b"fake-bytecode"})

    mod_id = mod_manager.install(archive, config=app_config, conn=conn)

    # Mods/<mod_id>/ is itself one level under Mods/, so a .ts4script must
    # land directly inside it (not one level further, which the game ignores).
    installed_path = app_config.sims4_mods_dir / mod_id / "mymod.ts4script"
    assert installed_path.is_file()
    assert not (app_config.sims4_mods_dir / mod_id / "Scripts").exists()


def test_install_accepts_package_at_any_depth(app_config, conn, tmp_path):
    archive = make_zip(
        tmp_path,
        "source.zip",
        {"root.package": b"a", "Tuning/deep/nested.package": b"b"},
    )

    mod_id = mod_manager.install(archive, config=app_config, conn=conn)

    mod_dir = app_config.sims4_mods_dir / mod_id
    assert (mod_dir / "root.package").is_file()
    assert (mod_dir / "Tuning" / "deep" / "nested.package").is_file()


def test_install_rejects_colliding_flattened_ts4script_names(app_config, conn, tmp_path):
    archive = make_zip(
        tmp_path,
        "source.zip",
        {"A/mymod.ts4script": b"one", "B/mymod.ts4script": b"two"},
    )

    with pytest.raises(mod_manager.ModManagerError):
        mod_manager.install(archive, config=app_config, conn=conn)

    assert conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 0
    assert list(app_config.library_dir.iterdir()) == []


def test_install_rejects_source_with_no_relevant_files(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"readme.txt": b"hi"})

    with pytest.raises(mod_manager.ModManagerError):
        mod_manager.install(archive, config=app_config, conn=conn)


# --- install pipeline -------------------------------------------------------


def test_install_records_db_row_and_files(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})

    mod_id = mod_manager.install(archive, config=app_config, conn=conn, mod_name="My Cool Mod")

    row = conn.execute("SELECT * FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["name"] == "My Cool Mod"
    assert row["primary_type"] == "package"
    assert row["active"] == 1

    files = conn.execute("SELECT * FROM mod_files WHERE mod_id = ?", (mod_id,)).fetchall()
    assert len(files) == 1
    assert files[0]["relative_path"] == "mymod.package"
    assert files[0]["hash"] == mod_manager.hash_file(
        app_config.library_dir / mod_id / "mymod.package"
    )


def test_install_mixed_type_mod(app_config, conn, tmp_path):
    archive = make_zip(
        tmp_path, "source.zip", {"mymod.package": b"a", "mymod.ts4script": b"b"}
    )

    mod_id = mod_manager.install(archive, config=app_config, conn=conn)

    row = conn.execute("SELECT primary_type FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["primary_type"] == "mixed"


def test_install_generates_unique_mod_id_on_name_collision(app_config, conn, tmp_path):
    archive1 = make_zip(tmp_path, "one.zip", {"mymod.package": b"a"})
    archive2 = make_zip(tmp_path, "two.zip", {"mymod.package": b"b"})

    id1 = mod_manager.install(archive1, config=app_config, conn=conn, mod_name="Same Name")
    id2 = mod_manager.install(archive2, config=app_config, conn=conn, mod_name="Same Name")

    assert id1 != id2


def test_install_uses_symlink_when_supported(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})

    mod_id = mod_manager.install(archive, config=app_config, conn=conn)

    assert (app_config.sims4_mods_dir / mod_id).is_symlink()


def test_install_copy_fallback_when_symlinks_unsupported(app_config, conn, tmp_path, monkeypatch):
    monkeypatch.setattr(type(app_config), "symlink_support", False, raising=False)
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})

    mod_id = mod_manager.install(archive, config=app_config, conn=conn)

    link = app_config.sims4_mods_dir / mod_id
    assert link.is_dir() and not link.is_symlink()
    assert (link / "mymod.package").is_file()


def test_install_from_bare_package_file(app_config, conn, tmp_path):
    bare = tmp_path / "standalone.package"
    bare.write_bytes(b"data")

    mod_id = mod_manager.install(bare, config=app_config, conn=conn)

    assert (app_config.sims4_mods_dir / mod_id / "standalone.package").is_file()


def test_install_rejects_unsupported_file_type(app_config, conn, tmp_path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("hello")

    with pytest.raises(mod_manager.ModManagerError):
        mod_manager.install(bogus, config=app_config, conn=conn)


# --- enable / disable / delete ---------------------------------------------


def test_disable_removes_symlink_but_keeps_library_files(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})
    mod_id = mod_manager.install(archive, config=app_config, conn=conn)

    mod_manager.disable(mod_id, config=app_config, conn=conn)

    assert not (app_config.sims4_mods_dir / mod_id).exists()
    assert (app_config.library_dir / mod_id / "mymod.package").is_file()
    row = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["active"] == 0


def test_enable_recreates_symlink(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})
    mod_id = mod_manager.install(archive, config=app_config, conn=conn)
    mod_manager.disable(mod_id, config=app_config, conn=conn)

    mod_manager.enable(mod_id, config=app_config, conn=conn)

    assert (app_config.sims4_mods_dir / mod_id / "mymod.package").is_file()
    row = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["active"] == 1


def test_delete_removes_library_files_and_db_row(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})
    mod_id = mod_manager.install(archive, config=app_config, conn=conn)

    mod_manager.delete(mod_id, config=app_config, conn=conn)

    assert not (app_config.sims4_mods_dir / mod_id).exists()
    assert not (app_config.library_dir / mod_id).exists()
    assert conn.execute("SELECT COUNT(*) FROM mods WHERE id = ?", (mod_id,)).fetchone()[0] == 0


def test_enable_unknown_mod_raises(app_config, conn):
    with pytest.raises(mod_manager.ModManagerError):
        mod_manager.enable("does-not-exist", config=app_config, conn=conn)


def test_disable_unknown_mod_raises(app_config, conn):
    with pytest.raises(mod_manager.ModManagerError):
        mod_manager.disable("does-not-exist", config=app_config, conn=conn)


def test_delete_unknown_mod_raises(app_config, conn):
    with pytest.raises(mod_manager.ModManagerError):
        mod_manager.delete("does-not-exist", config=app_config, conn=conn)


# --- import_existing_folder --------------------------------------------------


def test_import_existing_folder_adopts_into_library_and_symlinks_back(app_config, conn):
    preexisting = app_config.sims4_mods_dir / "SomeOldMod"
    preexisting.mkdir()
    (preexisting / "old.package").write_bytes(b"legacy-data")

    mod_id = mod_manager.import_existing_folder(preexisting, config=app_config, conn=conn)

    assert not preexisting.exists()
    link = app_config.sims4_mods_dir / mod_id
    assert link.is_symlink()
    assert (link / "old.package").is_file()
    assert (app_config.library_dir / mod_id / "old.package").is_file()


def test_import_existing_folder_flattens_nested_ts4script(app_config, conn):
    preexisting = app_config.sims4_mods_dir / "OldScriptMod"
    preexisting.mkdir()
    nested = preexisting / "Scripts"
    nested.mkdir()
    (nested / "old.ts4script").write_bytes(b"legacy-bytecode")

    mod_id = mod_manager.import_existing_folder(preexisting, config=app_config, conn=conn)

    link = app_config.sims4_mods_dir / mod_id
    assert (link / "old.ts4script").is_file()
    assert not (link / "Scripts").exists()
