import zipfile
from pathlib import Path

import pytest

from backend import mod_manager


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


def test_install_stores_curseforge_metadata_when_provided(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})
    metadata = mod_manager.ModMetadata(
        curseforge_id=111,
        author="SomeAuthor",
        category="Gameplay",
        installed_version="file_222",
        compat_status="compatible",
        short_description="Short",
        full_description="Full",
        thumbnail_url="https://example.com/thumb.png",
        links='{"curseforge_url": "https://www.curseforge.com/sims4/mods/x"}',
        game_version_min="1.90",
        game_version_max="1.110",
        third_party_distribution_allowed=True,
    )

    mod_id = mod_manager.install(archive, config=app_config, conn=conn, mod_name="X", metadata=metadata)

    row = conn.execute("SELECT * FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["curseforge_id"] == 111
    assert row["author"] == "SomeAuthor"
    assert row["compat_status"] == "compatible"
    assert row["third_party_distribution_allowed"] == 1


def test_install_without_metadata_defaults_compat_status_unknown(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})

    mod_id = mod_manager.install(archive, config=app_config, conn=conn)

    row = conn.execute("SELECT compat_status, curseforge_id FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["compat_status"] == "unknown"
    assert row["curseforge_id"] is None


def test_install_generates_unique_mod_id_on_name_collision(app_config, conn, tmp_path):
    archive1 = make_zip(tmp_path, "one.zip", {"mymod.package": b"a"})
    archive2 = make_zip(tmp_path, "two.zip", {"mymod.package": b"b"})

    id1 = mod_manager.install(archive1, config=app_config, conn=conn, mod_name="Same Name")
    id2 = mod_manager.install(archive2, config=app_config, conn=conn, mod_name="Same Name")

    assert id1 != id2


def test_regression_install_avoids_orphaned_library_folder_not_in_db(app_config, conn, tmp_path):
    # A library folder can outlive its DB row (e.g. the DB was reset/restored
    # separately from LIBRARY_DIR's contents). generate_unique_mod_id used to
    # only check the DB, so this orphaned folder wasn't seen as a collision,
    # and _finalize_install's plain mkdir(parents=True) raised an unhandled
    # FileExistsError instead of picking a fresh id.
    (app_config.library_dir / "mymod").mkdir()
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})

    mod_id = mod_manager.install(archive, config=app_config, conn=conn, mod_name="mymod")

    assert mod_id != "mymod"
    assert (app_config.library_dir / mod_id / "mymod.package").is_file()


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


def test_set_namespace_override_stores_the_value(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})
    mod_id = mod_manager.install(archive, config=app_config, conn=conn)

    mod_manager.set_namespace_override(mod_id, "Corrected Namespace", conn=conn)

    row = conn.execute("SELECT namespace_override FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["namespace_override"] == "Corrected Namespace"


def test_set_namespace_override_strips_whitespace(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})
    mod_id = mod_manager.install(archive, config=app_config, conn=conn)

    mod_manager.set_namespace_override(mod_id, "  Padded  ", conn=conn)

    row = conn.execute("SELECT namespace_override FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["namespace_override"] == "Padded"


def test_set_namespace_override_none_clears_it(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})
    mod_id = mod_manager.install(archive, config=app_config, conn=conn)
    mod_manager.set_namespace_override(mod_id, "Something", conn=conn)

    mod_manager.set_namespace_override(mod_id, None, conn=conn)

    row = conn.execute("SELECT namespace_override FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["namespace_override"] is None


def test_set_namespace_override_blank_string_clears_it(app_config, conn, tmp_path):
    archive = make_zip(tmp_path, "source.zip", {"mymod.package": b"data"})
    mod_id = mod_manager.install(archive, config=app_config, conn=conn)
    mod_manager.set_namespace_override(mod_id, "Something", conn=conn)

    mod_manager.set_namespace_override(mod_id, "   ", conn=conn)

    row = conn.execute("SELECT namespace_override FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["namespace_override"] is None


def test_set_namespace_override_unknown_mod_raises(app_config, conn):
    with pytest.raises(mod_manager.ModManagerError):
        mod_manager.set_namespace_override("does-not-exist", "X", conn=conn)


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
