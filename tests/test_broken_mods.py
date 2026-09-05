import dataclasses
import zipfile
from datetime import datetime

import pytest

from backend import backups as backups_module
from backend import broken_mods


class _FakeClock:
    """Distinct, increasing timestamps per call, so backups landing within
    the same wall-clock second don't collide (see test_download_watcher.py,
    which defines the same helper for the same reason)."""

    _seconds = 0

    @classmethod
    def now(cls, tz):
        cls._seconds += 1
        return datetime(2026, 1, 1, 0, 0, cls._seconds % 60, tzinfo=tz)


def _names_and_reasons(results):
    return {r.name: r.reason for r in results}


def test_empty_folder_is_reported_empty(app_config, conn):
    (app_config.sims4_mods_dir / "OptionalAddons").mkdir()

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert _names_and_reasons(results) == {"OptionalAddons": "empty"}
    assert results[0].file_count == 0


def test_loose_zip_is_reported_unextracted_archive(app_config, conn):
    folder = app_config.sims4_mods_dir / "SomeMod"
    folder.mkdir()
    (folder / "SomeMod.zip").write_bytes(b"pk-fake-zip-bytes")
    (folder / "readme.txt").write_bytes(b"read me")

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert _names_and_reasons(results) == {"SomeMod": "unextracted_archive"}
    assert results[0].zip_names == ["SomeMod.zip"]
    assert results[0].file_count == 2


def test_loose_pyc_files_are_reported_unpacked_script(app_config, conn):
    folder = app_config.sims4_mods_dir / "ExtractedScript"
    nested = folder / "mymod"
    nested.mkdir(parents=True)
    (nested / "__init__.pyc").write_bytes(b"compiled")
    (nested / "main.pyc").write_bytes(b"compiled")

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert _names_and_reasons(results) == {"ExtractedScript": "unpacked_script"}
    assert results[0].file_count == 2


def test_loose_py_source_is_reported_unpacked_script(app_config, conn):
    folder = app_config.sims4_mods_dir / "SourceOnlyMod"
    folder.mkdir()
    (folder / "mod.py").write_bytes(b"print('hi')")

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert _names_and_reasons(results) == {"SourceOnlyMod": "unpacked_script"}


def test_only_unrelated_files_reported_unrecognized(app_config, conn):
    folder = app_config.sims4_mods_dir / "Leftovers"
    folder.mkdir()
    (folder / "notes.log").write_bytes(b"log data")
    (folder / "warning.txt").write_bytes(b"do not use")

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert _names_and_reasons(results) == {"Leftovers": "unrecognized"}
    assert results[0].file_count == 2
    assert results[0].sample_files == ["notes.log", "warning.txt"]


def test_regression_sample_files_capped_and_sorted(app_config, conn):
    folder = app_config.sims4_mods_dir / "Leftovers"
    folder.mkdir()
    for i in range(8):
        (folder / f"file{i}.log").write_bytes(b"data")

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert len(results[0].sample_files) == 5
    assert results[0].sample_files == sorted(results[0].sample_files)


def test_sample_files_uses_relative_path_for_nested_files(app_config, conn):
    folder = app_config.sims4_mods_dir / "ExtractedScript"
    nested = folder / "mymod"
    nested.mkdir(parents=True)
    (nested / "main.pyc").write_bytes(b"compiled")

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert results[0].sample_files == ["mymod/main.pyc"]


def test_folder_with_package_file_is_not_reported(app_config, conn):
    folder = app_config.sims4_mods_dir / "RealMod"
    folder.mkdir()
    (folder / "real.package").write_bytes(b"data")

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert results == []


def test_folder_with_nested_ts4script_is_not_reported(app_config, conn):
    folder = app_config.sims4_mods_dir / "RealScriptMod"
    folder.mkdir()
    (folder / "real.ts4script").write_bytes(b"data")
    # Extra loose .pyc sitting alongside a real .ts4script shouldn't trip
    # 'unpacked_script' — the .ts4script itself is what the game loads.
    (folder / "leftover.pyc").write_bytes(b"stale")

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert results == []


def test_symlinked_managed_mod_is_not_reported(app_config, conn):
    library_target = app_config.library_dir / "managed-mod"
    library_target.mkdir()
    (app_config.sims4_mods_dir / "managed-mod").symlink_to(library_target, target_is_directory=True)

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert results == []


def test_known_mod_id_folder_is_not_reported_even_without_tracked_files(app_config, conn):
    # A managed mod's own library folder (accessed via its real, non-symlink
    # path rather than through Mods/) shouldn't be flagged just because it's
    # a known id with no .package/.ts4script directly inside it.
    conn.execute(
        "INSERT INTO mods (id, name, library_path, primary_type, install_date, active) "
        "VALUES ('known-mod', 'Known Mod', '/lib/known-mod', 'package', '2026-01-01', 1)"
    )
    conn.commit()
    (app_config.sims4_mods_dir / "known-mod").mkdir()

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert results == []


def test_no_mods_dir_returns_empty_list(app_config, conn):
    import shutil

    shutil.rmtree(app_config.sims4_mods_dir)

    assert broken_mods.scan_broken_mods(app_config, conn) == []


# --- fix_broken_mod ----------------------------------------------------------


def test_fix_empty_folder_deletes_it(app_config, conn):
    folder = app_config.sims4_mods_dir / "OptionalAddons"
    folder.mkdir()

    result = broken_mods.fix_broken_mod("OptionalAddons", app_config, conn)

    assert result is None
    assert not folder.exists()


def test_fix_unextracted_archive_installs_and_removes_original_folder(app_config, conn):
    folder = app_config.sims4_mods_dir / "SomeMod"
    folder.mkdir()
    archive = folder / "SomeMod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("mymod.package", b"data")

    mod_id = broken_mods.fix_broken_mod("SomeMod", app_config, conn)

    assert mod_id is not None
    assert not folder.exists()
    row = conn.execute("SELECT name, active FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["name"] == "SomeMod"
    assert row["active"] == 1
    assert (app_config.sims4_mods_dir / mod_id).is_symlink()


def test_fix_backs_up_original_folder_before_deleting(app_config, conn):
    # A backup lets the user manually roll back if a fix turns out to be
    # wrong (e.g. the wrong archive was auto-extracted) — same reasoning as
    # download_watcher.py backing up before a replace.
    folder = app_config.sims4_mods_dir / "SomeMod"
    folder.mkdir()
    archive = folder / "SomeMod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("mymod.package", b"original-bytes")

    broken_mods.fix_broken_mod("SomeMod", app_config, conn)

    backups = list((app_config.library_dir / ".backups").glob("SomeMod-*"))
    assert len(backups) == 1
    assert (backups[0] / "SomeMod.zip").is_file()


def test_fix_respects_backup_retention_count(app_config, conn, monkeypatch):
    monkeypatch.setattr(backups_module, "datetime", _FakeClock)
    limited_config = dataclasses.replace(app_config, backup_retention_count=1)
    for i in range(3):
        folder = limited_config.sims4_mods_dir / "RepeatedMod"
        folder.mkdir()
        with zipfile.ZipFile(folder / f"v{i}.zip", "w") as zf:
            zf.writestr("mod.package", b"data")
        broken_mods.fix_broken_mod("RepeatedMod", limited_config, conn)

    backups = sorted((limited_config.library_dir / ".backups").glob("RepeatedMod-*"))
    assert len(backups) == 1


def test_fix_unextracted_archive_with_multiple_zips_raises(app_config, conn):
    folder = app_config.sims4_mods_dir / "ChooseOne"
    folder.mkdir()
    with zipfile.ZipFile(folder / "OptionA.zip", "w") as zf:
        zf.writestr("a.package", b"a")
    with zipfile.ZipFile(folder / "OptionB.zip", "w") as zf:
        zf.writestr("b.package", b"b")

    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.fix_broken_mod("ChooseOne", app_config, conn)

    assert folder.exists()


def test_fix_unpacked_script_raises_no_automatic_fix(app_config, conn):
    folder = app_config.sims4_mods_dir / "ExtractedScript"
    folder.mkdir()
    (folder / "main.pyc").write_bytes(b"compiled")

    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.fix_broken_mod("ExtractedScript", app_config, conn)

    assert folder.exists()


def test_fix_unrecognized_raises_no_automatic_fix(app_config, conn):
    folder = app_config.sims4_mods_dir / "Leftovers"
    folder.mkdir()
    (folder / "notes.log").write_bytes(b"log")

    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.fix_broken_mod("Leftovers", app_config, conn)


def test_fix_unknown_folder_raises(app_config, conn):
    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.fix_broken_mod("DoesNotExist", app_config, conn)


# --- attempt_script_repair ----------------------------------------------------


def test_attempt_script_repair_rebuilds_and_installs_ts4script(app_config, conn):
    folder = app_config.sims4_mods_dir / "ExtractedScript"
    nested = folder / "mymod"
    nested.mkdir(parents=True)
    (nested / "__init__.pyc").write_bytes(b"compiled-init")
    (nested / "main.pyc").write_bytes(b"compiled-main")

    mod_id = broken_mods.attempt_script_repair("ExtractedScript", app_config, conn)

    assert mod_id is not None
    assert not folder.exists()
    row = conn.execute("SELECT name, active, primary_type FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["name"] == "ExtractedScript"
    assert row["active"] == 1
    assert row["primary_type"] == "script"
    # The rebuilt .ts4script must preserve the package's internal layout
    # (the folder name as the zip's own root), not flatten it away.
    file_rows = conn.execute(
        "SELECT relative_path FROM mod_files WHERE mod_id = ?", (mod_id,)
    ).fetchall()
    ts4script_path = next(
        app_config.library_dir.glob(f"{mod_id}/**/*.ts4script")
    )
    with zipfile.ZipFile(ts4script_path) as archive:
        names = sorted(archive.namelist())
    assert names == ["mymod/__init__.pyc", "mymod/main.pyc"]
    assert len(file_rows) == 1  # the .ts4script itself, flattened to mod root


def test_attempt_script_repair_backs_up_original_folder(app_config, conn, monkeypatch):
    monkeypatch.setattr(backups_module, "datetime", _FakeClock)
    folder = app_config.sims4_mods_dir / "ExtractedScript"
    folder.mkdir()
    (folder / "main.pyc").write_bytes(b"compiled")

    broken_mods.attempt_script_repair("ExtractedScript", app_config, conn)

    backups = list((app_config.library_dir / ".backups").glob("ExtractedScript-*"))
    assert len(backups) == 1
    assert (backups[0] / "main.pyc").is_file()


def test_attempt_script_repair_rejects_non_script_reason(app_config, conn):
    folder = app_config.sims4_mods_dir / "OptionalAddons"
    folder.mkdir()

    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.attempt_script_repair("OptionalAddons", app_config, conn)

    assert folder.exists()


def test_attempt_script_repair_unknown_folder_raises(app_config, conn):
    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.attempt_script_repair("DoesNotExist", app_config, conn)


def test_delete_broken_folder_removes_it(app_config):
    folder = app_config.sims4_mods_dir / "SomeExtractedScript"
    (folder / "pkg").mkdir(parents=True)
    (folder / "pkg" / "main.pyc").write_bytes(b"data")

    broken_mods.delete_broken_folder("SomeExtractedScript", app_config)

    assert not folder.exists()


def test_delete_broken_folder_backs_up_before_deleting(app_config):
    folder = app_config.sims4_mods_dir / "SomeExtractedScript"
    (folder / "pkg").mkdir(parents=True)
    (folder / "pkg" / "main.pyc").write_bytes(b"original-bytes")

    broken_mods.delete_broken_folder("SomeExtractedScript", app_config)

    backups = list((app_config.library_dir / ".backups").glob("SomeExtractedScript-*"))
    assert len(backups) == 1
    assert (backups[0] / "pkg" / "main.pyc").read_bytes() == b"original-bytes"


def test_delete_broken_folder_works_for_any_reason(app_config):
    # Unlike fix_broken_mod()/attempt_script_repair(), this isn't restricted
    # to specific reasons — it's a generic "get rid of this" action.
    folder = app_config.sims4_mods_dir / "JustJunk"
    (folder / "readme.txt").parent.mkdir(parents=True)
    (folder / "readme.txt").write_text("leftover")

    broken_mods.delete_broken_folder("JustJunk", app_config)

    assert not folder.exists()


def test_delete_broken_folder_unknown_folder_raises(app_config):
    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.delete_broken_folder("DoesNotExist", app_config)
