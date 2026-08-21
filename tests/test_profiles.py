import zipfile
from pathlib import Path

import pytest

from backend import dependencies as deps
from backend import mod_manager
from backend import profiles


def _install(app_config, conn, tmp_path, name, filename="mymod.package") -> str:
    archive = tmp_path / f"{name}-src.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename, b"data")
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name)


def test_create_profile_returns_id(conn):
    profile_id = profiles.create_profile("Build Only", conn)

    assert isinstance(profile_id, int)


def test_create_profile_rejects_duplicate_name(conn):
    profiles.create_profile("Build Only", conn)

    with pytest.raises(profiles.ProfileError):
        profiles.create_profile("Build Only", conn)


def test_create_profile_rejects_empty_name(conn):
    with pytest.raises(profiles.ProfileError):
        profiles.create_profile("   ", conn)


def test_list_profiles_includes_membership(app_config, conn, tmp_path):
    mod_id = _install(app_config, conn, tmp_path, "Mod A")
    profile_id = profiles.create_profile("Build Only", conn)
    profiles.set_profile_mods(profile_id, [mod_id], conn)

    result = profiles.list_profiles(conn)

    assert len(result) == 1
    assert result[0].name == "Build Only"
    assert result[0].mod_ids == [mod_id]


def test_get_profile_unknown_raises(conn):
    with pytest.raises(profiles.ProfileError):
        profiles.get_profile(999, conn)


def test_set_profile_mods_replaces_membership_wholesale(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A", filename="a.package")
    mod_b = _install(app_config, conn, tmp_path, "Mod B", filename="b.package")
    profile_id = profiles.create_profile("Build Only", conn)

    profiles.set_profile_mods(profile_id, [mod_a, mod_b], conn)
    profiles.set_profile_mods(profile_id, [mod_b], conn)  # replaces, doesn't append

    assert profiles.get_profile(profile_id, conn).mod_ids == [mod_b]


def test_delete_profile_removes_it_and_membership(app_config, conn, tmp_path):
    mod_id = _install(app_config, conn, tmp_path, "Mod A")
    profile_id = profiles.create_profile("Build Only", conn)
    profiles.set_profile_mods(profile_id, [mod_id], conn)

    profiles.delete_profile(profile_id, conn)

    assert profiles.list_profiles(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM profile_mods").fetchone()[0] == 0


# --- activation ----------------------------------------------------------------


def test_activate_profile_enables_membership_and_disables_the_rest(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A", filename="a.package")
    mod_b = _install(app_config, conn, tmp_path, "Mod B", filename="b.package")
    profile_id = profiles.create_profile("Build Only", conn)
    profiles.set_profile_mods(profile_id, [mod_a], conn)

    profiles.activate_profile(profile_id, config=app_config, conn=conn)

    row_a = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_a,)).fetchone()
    row_b = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_b,)).fetchone()
    assert row_a["active"] == 1
    assert row_b["active"] == 0
    assert (app_config.sims4_mods_dir / mod_a).exists()
    assert not (app_config.sims4_mods_dir / mod_b).exists()


def test_activate_profile_is_a_noop_for_mods_already_in_the_right_state(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A")  # installed active by default
    profile_id = profiles.create_profile("Everything", conn)
    profiles.set_profile_mods(profile_id, [mod_a], conn)

    profiles.activate_profile(profile_id, config=app_config, conn=conn)  # must not raise

    assert conn.execute("SELECT active FROM mods WHERE id = ?", (mod_a,)).fetchone()["active"] == 1


def test_activate_profile_fails_fast_on_unresolved_required_dependency(app_config, conn, tmp_path):
    core_id = _install(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    mod_manager.disable(core_id, config=app_config, conn=conn)
    dependent_id = _install(app_config, conn, tmp_path, "Needs Core", filename="needs.package")
    mod_manager.disable(dependent_id, config=app_config, conn=conn)
    deps.add_dependency(dependent_id, conn=conn, dependency_type="required", depends_on_mod_id=core_id)

    profile_id = profiles.create_profile("Broken", conn)
    profiles.set_profile_mods(profile_id, [dependent_id], conn)  # core_id NOT included

    with pytest.raises(deps.UnresolvedRequiredDependencyError):
        profiles.activate_profile(profile_id, config=app_config, conn=conn)
