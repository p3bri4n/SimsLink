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


def test_create_profile_sets_created_date(conn):
    profile_id = profiles.create_profile("Build Only", conn)

    profile = profiles.get_profile(profile_id, conn)

    assert profile.created_date
    # Must be a real, parseable UTC timestamp, not just a truthy placeholder.
    from datetime import datetime

    datetime.fromisoformat(profile.created_date)


def test_list_profiles_orders_most_recent_first(conn, monkeypatch):
    from datetime import datetime, timezone

    class _FakeClock:
        _seconds = 0

        @classmethod
        def now(cls, tz):
            cls._seconds += 1
            return datetime(2026, 1, 1, 0, 0, cls._seconds, tzinfo=tz)

    monkeypatch.setattr(profiles, "datetime", _FakeClock)
    profiles.create_profile("First", conn)
    profiles.create_profile("Second", conn)

    result = profiles.list_profiles(conn)

    assert [p.name for p in result] == ["Second", "First"]


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


def test_activate_profile_enables_membership(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A", filename="a.package")
    mod_manager.disable(mod_a, config=app_config, conn=conn)
    profile_id = profiles.create_profile("Build Only", conn)
    profiles.set_profile_mods(profile_id, [mod_a], conn)

    profiles.activate_profile(profile_id, config=app_config, conn=conn)

    row_a = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_a,)).fetchone()
    assert row_a["active"] == 1
    assert (app_config.sims4_mods_dir / mod_a).exists()


def test_regression_activate_profile_does_not_disable_mods_outside_the_snapshot(app_config, conn, tmp_path):
    # A mod installed *after* a save was taken (e.g. by extracting an
    # archive through broken_mods.py, or just adding a new mod normally)
    # must survive loading an older save untouched — a "restore point"
    # shouldn't have the side effect of disabling something added since.
    mod_a = _install(app_config, conn, tmp_path, "Mod A", filename="a.package")
    profile_id = profiles.create_profile("Build Only", conn)
    profiles.set_profile_mods(profile_id, [mod_a], conn)  # snapshot taken before Mod B existed

    mod_b = _install(app_config, conn, tmp_path, "Mod B", filename="b.package")  # active by default

    profiles.activate_profile(profile_id, config=app_config, conn=conn)

    row_b = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_b,)).fetchone()
    assert row_b["active"] == 1
    assert (app_config.sims4_mods_dir / mod_b).exists()


def test_activate_profile_is_a_noop_for_mods_already_in_the_right_state(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A")  # installed active by default
    profile_id = profiles.create_profile("Everything", conn)
    profiles.set_profile_mods(profile_id, [mod_a], conn)

    profiles.activate_profile(profile_id, config=app_config, conn=conn)  # must not raise

    assert conn.execute("SELECT active FROM mods WHERE id = ?", (mod_a,)).fetchone()["active"] == 1


def test_activate_profile_skips_a_mod_id_that_no_longer_exists(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A")
    profile_id = profiles.create_profile("Stale", conn)
    profiles.set_profile_mods(profile_id, [mod_a], conn)
    mod_manager.delete(mod_a, config=app_config, conn=conn)

    profiles.activate_profile(profile_id, config=app_config, conn=conn)  # must not raise


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


# --- record_missing_mod_if_saved() / list_missing_mods() / dismiss_missing_mod() -----


def test_record_missing_mod_noop_when_mod_in_no_profile(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A")

    profiles.record_missing_mod_if_saved(mod_a, conn)

    assert profiles.list_missing_mods(conn) == []


def test_record_missing_mod_creates_reminder_when_mod_is_saved(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A")
    profile_id = profiles.create_profile("Weekend Build", conn)
    profiles.set_profile_mods(profile_id, [mod_a], conn)

    # Must run before the actual delete — see the docstring on why.
    profiles.record_missing_mod_if_saved(mod_a, conn)
    mod_manager.delete(mod_a, config=app_config, conn=conn)

    missing = profiles.list_missing_mods(conn)
    assert len(missing) == 1
    assert missing[0].mod_id == mod_a
    assert missing[0].name == "Mod A"
    assert missing[0].source_profile_names == "Weekend Build"
    assert missing[0].curseforge_url is None


def test_record_missing_mod_captures_curseforge_url(app_config, conn, tmp_path):
    archive = tmp_path / "linked-src.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("linked.package", b"data")
    metadata = mod_manager.ModMetadata(links='{"curseforge_url": "https://www.curseforge.com/sims4/mods/linked"}')
    mod_id = mod_manager.install(archive, config=app_config, conn=conn, mod_name="Linked Mod", metadata=metadata)
    profile_id = profiles.create_profile("Save", conn)
    profiles.set_profile_mods(profile_id, [mod_id], conn)

    profiles.record_missing_mod_if_saved(mod_id, conn)
    mod_manager.delete(mod_id, config=app_config, conn=conn)

    missing = profiles.list_missing_mods(conn)
    assert missing[0].curseforge_url == "https://www.curseforge.com/sims4/mods/linked"


def test_record_missing_mod_dedupes_across_profiles(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A")
    profile_1 = profiles.create_profile("Save One", conn)
    profiles.set_profile_mods(profile_1, [mod_a], conn)
    profile_2 = profiles.create_profile("Save Two", conn)
    profiles.set_profile_mods(profile_2, [mod_a], conn)

    profiles.record_missing_mod_if_saved(mod_a, conn)
    mod_manager.delete(mod_a, config=app_config, conn=conn)

    missing = profiles.list_missing_mods(conn)
    assert len(missing) == 1
    assert missing[0].source_profile_names == "Save One, Save Two"


def test_record_missing_mod_is_idempotent(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A")
    profile_id = profiles.create_profile("Save", conn)
    profiles.set_profile_mods(profile_id, [mod_a], conn)

    profiles.record_missing_mod_if_saved(mod_a, conn)
    profiles.record_missing_mod_if_saved(mod_a, conn)  # called twice, e.g. a retried request
    mod_manager.delete(mod_a, config=app_config, conn=conn)

    assert len(profiles.list_missing_mods(conn)) == 1


def test_regression_replacing_a_saved_mod_does_not_create_a_missing_reminder(app_config, conn, tmp_path):
    # download_watcher.confirm_replace() (and broken_mods.fix_rezipped_mod())
    # call mod_manager.delete() then mod_manager.install() as a single
    # replace step — that's an update, not a real removal, and must never
    # produce a "you might want to reinstall this" reminder. Guards against
    # a future change accidentally moving the record_missing_mod_if_saved()
    # hook into mod_manager.delete() itself instead of the explicit
    # DELETE /api/mods/{mod_id} route.
    from backend import download_watcher

    mod_a = _install(app_config, conn, tmp_path, "Mod A")
    profile_id = profiles.create_profile("Save", conn)
    profiles.set_profile_mods(profile_id, [mod_a], conn)

    new_archive = tmp_path / "update-src.zip"
    with zipfile.ZipFile(new_archive, "w") as zf:
        zf.writestr("mymod.package", b"updated-data")
    download_watcher.confirm_replace(new_archive, mod_a, config=app_config, conn=conn)

    assert profiles.list_missing_mods(conn) == []


def test_list_missing_mods_excludes_a_mod_that_reappeared(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A")
    profile_id = profiles.create_profile("Save", conn)
    profiles.set_profile_mods(profile_id, [mod_a], conn)
    profiles.record_missing_mod_if_saved(mod_a, conn)
    mod_manager.delete(mod_a, config=app_config, conn=conn)
    assert len(profiles.list_missing_mods(conn)) == 1

    # Reinstalling under the same name frees back the exact same slug/id.
    _install(app_config, conn, tmp_path, "Mod A")

    assert profiles.list_missing_mods(conn) == []


def test_dismiss_missing_mod_removes_it(app_config, conn, tmp_path):
    mod_a = _install(app_config, conn, tmp_path, "Mod A")
    profile_id = profiles.create_profile("Save", conn)
    profiles.set_profile_mods(profile_id, [mod_a], conn)
    profiles.record_missing_mod_if_saved(mod_a, conn)
    mod_manager.delete(mod_a, config=app_config, conn=conn)
    entry_id = profiles.list_missing_mods(conn)[0].id

    profiles.dismiss_missing_mod(entry_id, conn)

    assert profiles.list_missing_mods(conn) == []
