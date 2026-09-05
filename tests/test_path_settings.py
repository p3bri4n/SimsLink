import zipfile
from pathlib import Path

import pytest

from backend import mod_manager
from backend import path_settings


def _install(app_config, conn, tmp_path, name, filename="mymod.package") -> str:
    archive = tmp_path / f"{name}-src.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename, b"data")
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name)


# --- validate_paths() ---------------------------------------------------------------


def test_validate_paths_raises_when_two_paths_are_identical(app_config):
    with pytest.raises(path_settings.PathValidationError):
        path_settings.validate_paths(app_config.sims4_game_dir, app_config.sims4_user_dir, app_config.sims4_user_dir)


def test_validate_paths_raises_when_library_inside_mods_dir(app_config):
    library_dir = app_config.sims4_mods_dir / "nested-library"

    with pytest.raises(path_settings.PathValidationError):
        path_settings.validate_paths(app_config.sims4_game_dir, app_config.sims4_user_dir, library_dir)


def test_validate_paths_raises_when_mods_dir_inside_library(app_config, tmp_path):
    # A user dir whose Mods/ would land inside the library folder — the
    # same recursive-symlink risk, the other way around.
    library_dir = tmp_path / "library"
    user_dir = library_dir / "somewhere"

    with pytest.raises(path_settings.PathValidationError):
        path_settings.validate_paths(app_config.sims4_game_dir, user_dir, library_dir)


def test_validate_paths_warns_when_game_dir_has_no_recognizable_executable(app_config):
    warnings = path_settings.validate_paths(
        app_config.sims4_game_dir, app_config.sims4_user_dir, app_config.library_dir
    )

    assert len(warnings) == 1


def test_validate_paths_no_warning_when_game_executable_present(app_config):
    bin_dir = app_config.sims4_game_dir / "Game" / "Bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "TS4_x64.exe").write_bytes(b"fake-exe")

    warnings = path_settings.validate_paths(
        app_config.sims4_game_dir, app_config.sims4_user_dir, app_config.library_dir
    )

    assert warnings == []


# --- apply_stored_overrides() -------------------------------------------------------


def test_apply_stored_overrides_returns_unchanged_config_with_no_settings_rows(app_config, conn):
    result = path_settings.apply_stored_overrides(app_config, conn)

    assert result == app_config


def test_apply_stored_overrides_applies_a_stored_library_dir(app_config, conn, tmp_path):
    new_library = tmp_path / "new-library"
    conn.execute("INSERT INTO settings (key, value) VALUES ('library_dir', ?)", (str(new_library),))
    conn.commit()

    result = path_settings.apply_stored_overrides(app_config, conn)

    assert result.library_dir == new_library
    assert result.sims4_game_dir == app_config.sims4_game_dir  # untouched fields survive


# --- update_paths() ------------------------------------------------------------------


def test_update_paths_persists_to_settings_table(app_config, conn, tmp_path):
    new_library = tmp_path / "new-library"

    path_settings.update_paths(app_config, conn, library_dir=new_library)

    row = conn.execute("SELECT value FROM settings WHERE key = 'library_dir'").fetchone()
    assert row["value"] == str(new_library)


def test_update_paths_rejects_an_incoherent_combination_and_persists_nothing(app_config, conn):
    bad_library = app_config.sims4_mods_dir / "nested"

    with pytest.raises(path_settings.PathValidationError):
        path_settings.update_paths(app_config, conn, library_dir=bad_library)

    assert conn.execute("SELECT * FROM settings").fetchall() == []


def test_update_paths_moves_library_contents_and_updates_mod_rows(app_config, conn, tmp_path):
    mod_id = _install(app_config, conn, tmp_path, "Mod A")
    new_library = tmp_path / "new-library"

    new_config, warnings = path_settings.update_paths(app_config, conn, library_dir=new_library)

    assert not (app_config.library_dir / mod_id).exists()
    assert (new_library / mod_id / "mymod.package").is_file()
    row = conn.execute("SELECT library_path FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["library_path"] == str(new_library / mod_id)
    assert new_config.library_dir == new_library


def test_update_paths_relinks_active_mods_to_the_new_library(app_config, conn, tmp_path):
    mod_id = _install(app_config, conn, tmp_path, "Mod A")
    new_library = tmp_path / "new-library"

    new_config, _ = path_settings.update_paths(app_config, conn, library_dir=new_library)

    link = new_config.sims4_mods_dir / mod_id
    assert link.is_symlink()
    assert link.resolve() == (new_library / mod_id).resolve()


def test_update_paths_does_not_relink_a_disabled_mod(app_config, conn, tmp_path):
    mod_id = _install(app_config, conn, tmp_path, "Mod A")
    mod_manager.disable(mod_id, config=app_config, conn=conn)
    new_library = tmp_path / "new-library"

    new_config, _ = path_settings.update_paths(app_config, conn, library_dir=new_library)

    assert not (new_config.sims4_mods_dir / mod_id).exists()


def test_update_paths_does_not_touch_library_when_only_game_dir_changes(app_config, conn, tmp_path):
    mod_id = _install(app_config, conn, tmp_path, "Mod A")
    original_library_path = conn.execute(
        "SELECT library_path FROM mods WHERE id = ?", (mod_id,)
    ).fetchone()["library_path"]
    new_game_dir = tmp_path / "new-game-dir"
    new_game_dir.mkdir()

    path_settings.update_paths(app_config, conn, sims4_game_dir=new_game_dir)

    assert (app_config.library_dir / mod_id).is_dir()  # never moved
    row = conn.execute("SELECT library_path FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["library_path"] == original_library_path


def test_update_paths_relinks_when_only_user_dir_changes(app_config, conn, tmp_path):
    mod_id = _install(app_config, conn, tmp_path, "Mod A")
    new_user_dir = tmp_path / "new-sims4user"
    new_user_dir.mkdir()

    new_config, _ = path_settings.update_paths(app_config, conn, sims4_user_dir=new_user_dir)

    # Library itself never moved...
    assert (app_config.library_dir / mod_id).is_dir()
    # ...but the symlink now lives under the new Mods/ folder and still
    # resolves to the (unmoved) library content.
    new_link = new_config.sims4_mods_dir / mod_id
    assert new_link.is_symlink()
    assert new_link.resolve() == (app_config.library_dir / mod_id).resolve()
    assert not (app_config.sims4_mods_dir / mod_id).exists()


def test_update_paths_partial_update_keeps_other_fields(app_config, conn, tmp_path):
    new_game_dir = tmp_path / "new-game-dir"
    new_game_dir.mkdir()

    new_config, _ = path_settings.update_paths(app_config, conn, sims4_game_dir=new_game_dir)

    assert new_config.sims4_game_dir == new_game_dir
    assert new_config.sims4_user_dir == app_config.sims4_user_dir
    assert new_config.library_dir == app_config.library_dir
