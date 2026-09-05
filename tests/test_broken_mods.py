import dataclasses
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from backend import backups as backups_module
from backend import broken_mods
from backend import mod_manager


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
    assert results[0].zip_paths == ["SomeMod.zip"]
    assert results[0].file_count == 2


def test_nested_zip_is_reported_with_relative_path(app_config, conn):
    # A zip doesn't have to sit at the folder's root — _classify() scans
    # recursively, and the extraction logic needs the full relative path
    # to find it again, not just its bare filename (a bare name silently
    # resolved to the wrong location for anything not at the root).
    folder = app_config.sims4_mods_dir / "SomeMod"
    nested = folder / "Optional"
    nested.mkdir(parents=True)
    (nested / "Variant.zip").write_bytes(b"pk-fake-zip-bytes")

    results = broken_mods.scan_broken_mods(app_config, conn)

    assert results[0].zip_paths == ["Optional/Variant.zip"]


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


# --- extract_selected_zips (multi-archive choice) ----------------------------


def test_extract_selected_zips_installs_one_of_several(app_config, conn):
    folder = app_config.sims4_mods_dir / "ChooseOne"
    folder.mkdir()
    with zipfile.ZipFile(folder / "OptionA.zip", "w") as zf:
        zf.writestr("a.package", b"a")
    with zipfile.ZipFile(folder / "OptionB.zip", "w") as zf:
        zf.writestr("b.package", b"b")

    result = broken_mods.extract_selected_zips("ChooseOne", app_config, conn, ["OptionA.zip"])

    assert len(result["installed"]) == 1
    assert result["deferred"] == []
    assert not folder.exists()
    row = conn.execute("SELECT active FROM mods WHERE id = ?", (result["installed"][0],)).fetchone()
    assert row["active"] == 1
    # OptionB.zip was never selected — still recoverable from the backup,
    # not silently lost.
    backups = list((app_config.library_dir / ".backups").glob("ChooseOne-*"))
    assert (backups[0] / "OptionB.zip").is_file()


def test_extract_selected_zips_installs_several_as_separate_mods(app_config, conn):
    folder = app_config.sims4_mods_dir / "NeedsBoth"
    folder.mkdir()
    with zipfile.ZipFile(folder / "Main.zip", "w") as zf:
        zf.writestr("main.package", b"main")
    with zipfile.ZipFile(folder / "Fix.zip", "w") as zf:
        zf.writestr("fix.package", b"fix")

    result = broken_mods.extract_selected_zips("NeedsBoth", app_config, conn, ["Main.zip", "Fix.zip"])

    assert len(result["installed"]) == 2
    names = {
        conn.execute("SELECT name FROM mods WHERE id = ?", (mid,)).fetchone()["name"] for mid in result["installed"]
    }
    assert names == {"NeedsBoth - Main", "NeedsBoth - Fix"}


def test_extract_selected_zips_finds_nested_archive_by_relative_path(app_config, conn):
    folder = app_config.sims4_mods_dir / "SomeMod"
    nested = folder / "Optional"
    nested.mkdir(parents=True)
    with zipfile.ZipFile(nested / "Variant.zip", "w") as zf:
        zf.writestr("variant.package", b"data")

    result = broken_mods.extract_selected_zips("SomeMod", app_config, conn, ["Optional/Variant.zip"])

    assert len(result["installed"]) == 1


def test_extract_selected_zips_defers_a_zip_of_zips(app_config, conn):
    # A selected archive that contains only further archives (no directly
    # loadable content of its own) can't be installed as a mod outright —
    # it's extracted into a fresh Mods/ folder instead, so the next scan
    # reports it as a new 'unextracted_archive' entry to choose from again.
    folder = app_config.sims4_mods_dir / "OuterZip"
    folder.mkdir()
    outer = folder / "Outer.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        # Write inner .zip *bytes* as an entry — a real nested archive.
        inner_bytes_path = app_config.sims4_mods_dir / "_inner.zip"
        with zipfile.ZipFile(inner_bytes_path, "w") as inner:
            inner.writestr("real.package", b"data")
        zf.write(inner_bytes_path, "Inner.zip")
        inner_bytes_path.unlink()

    result = broken_mods.extract_selected_zips("OuterZip", app_config, conn, ["Outer.zip"])

    assert result["installed"] == []
    assert len(result["deferred"]) == 1
    deferred_folder = app_config.sims4_mods_dir / result["deferred"][0]
    assert deferred_folder.is_dir()
    assert (deferred_folder / "Inner.zip").is_file()
    assert not folder.exists()

    # The deferred folder is a real, freshly extracted Mods/ folder — the
    # next scan picks it up as its own 'unextracted_archive' entry.
    rescanned = broken_mods.scan_broken_mods(app_config, conn)
    assert any(r.name == result["deferred"][0] and r.reason == "unextracted_archive" for r in rescanned)


def test_extract_selected_zips_rejects_empty_selection(app_config, conn):
    folder = app_config.sims4_mods_dir / "ChooseOne"
    folder.mkdir()
    with zipfile.ZipFile(folder / "OptionA.zip", "w") as zf:
        zf.writestr("a.package", b"a")

    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.extract_selected_zips("ChooseOne", app_config, conn, [])

    assert folder.exists()


def test_extract_selected_zips_rejects_unknown_archive_path(app_config, conn):
    folder = app_config.sims4_mods_dir / "ChooseOne"
    folder.mkdir()
    with zipfile.ZipFile(folder / "OptionA.zip", "w") as zf:
        zf.writestr("a.package", b"a")

    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.extract_selected_zips("ChooseOne", app_config, conn, ["DoesNotExist.zip"])

    assert folder.exists()


def test_extract_selected_zips_rejects_non_archive_reason(app_config, conn):
    folder = app_config.sims4_mods_dir / "OptionalAddons"
    folder.mkdir()

    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.extract_selected_zips("OptionalAddons", app_config, conn, ["whatever.zip"])


def _install_real_mod(app_config, conn, name="RealMod") -> str:
    source = app_config.download_watch_dir
    source.mkdir(parents=True, exist_ok=True)
    package = source / f"{name}.package"
    package.write_bytes(b"fake-dbpf-bytes")
    return mod_manager.install(package, config=app_config, conn=conn, mod_name=name)


def _rezip_in_place(app_config, mod_id: str, conn, zip_name: str = "Rezipped.zip") -> None:
    """Simulates a manual "dezip then rezip directly in Mods/" edit: the
    tracked mod's real library folder is emptied of loadable content and a
    plain .zip containing fresh .package data is dropped in — exactly what
    lands there since Mods/<mod_id>/ is a symlink into this same folder."""
    library_path = Path(
        conn.execute("SELECT library_path FROM mods WHERE id = ?", (mod_id,)).fetchone()["library_path"]
    )
    for f in library_path.rglob("*"):
        if f.is_file():
            f.unlink()
    with zipfile.ZipFile(library_path / zip_name, "w") as zf:
        zf.writestr("rezipped.package", b"rezipped-data")


def test_rezipped_mod_is_detected_when_content_replaced_by_zip(app_config, conn):
    mod_id = _install_real_mod(app_config, conn)
    _rezip_in_place(app_config, mod_id, conn)

    results = broken_mods.scan_rezipped_mods(app_config, conn)

    assert len(results) == 1
    assert results[0].mod_id == mod_id
    assert results[0].zip_paths == ["Rezipped.zip"]


def test_normally_installed_mod_is_not_reported_as_rezipped(app_config, conn):
    _install_real_mod(app_config, conn)

    assert broken_mods.scan_rezipped_mods(app_config, conn) == []


def test_mod_with_loadable_content_alongside_a_stray_zip_is_not_reported(app_config, conn):
    # Still has a real .package the game will load — a zip sitting next to
    # it isn't "rezipped," it's just extra clutter, out of scope here.
    mod_id = _install_real_mod(app_config, conn)
    library_path = Path(
        conn.execute("SELECT library_path FROM mods WHERE id = ?", (mod_id,)).fetchone()["library_path"]
    )
    with zipfile.ZipFile(library_path / "Extra.zip", "w") as zf:
        zf.writestr("extra.package", b"extra")

    assert broken_mods.scan_rezipped_mods(app_config, conn) == []


def test_fix_rezipped_mod_reinstalls_from_the_zip(app_config, conn):
    mod_id = _install_real_mod(app_config, conn)
    _rezip_in_place(app_config, mod_id, conn)

    new_mod_id = broken_mods.fix_rezipped_mod(mod_id, app_config, conn)

    row = conn.execute("SELECT * FROM mods WHERE id = ?", (new_mod_id,)).fetchone()
    assert row is not None
    assert row["active"] == 1
    library_path = Path(row["library_path"])
    assert (library_path / "rezipped.package").is_file()
    assert broken_mods.scan_rezipped_mods(app_config, conn) == []


def test_fix_rezipped_mod_preserves_the_mod_id(app_config, conn):
    mod_id = _install_real_mod(app_config, conn, name="RealMod")
    _rezip_in_place(app_config, mod_id, conn)

    new_mod_id = broken_mods.fix_rezipped_mod(mod_id, app_config, conn)

    assert new_mod_id == mod_id


def test_fix_rezipped_mod_backs_up_original_folder_before_deleting(app_config, conn, monkeypatch):
    monkeypatch.setattr(backups_module, "datetime", _FakeClock)
    mod_id = _install_real_mod(app_config, conn)
    _rezip_in_place(app_config, mod_id, conn)

    broken_mods.fix_rezipped_mod(mod_id, app_config, conn)

    backups = list((app_config.library_dir / ".backups").glob(f"{mod_id}-*"))
    assert len(backups) == 1
    assert (backups[0] / "Rezipped.zip").is_file()


def test_fix_rezipped_mod_raises_when_multiple_zips_present(app_config, conn):
    mod_id = _install_real_mod(app_config, conn)
    _rezip_in_place(app_config, mod_id, conn, zip_name="First.zip")
    library_path = Path(
        conn.execute("SELECT library_path FROM mods WHERE id = ?", (mod_id,)).fetchone()["library_path"]
    )
    with zipfile.ZipFile(library_path / "Second.zip", "w") as zf:
        zf.writestr("second.package", b"second")

    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.fix_rezipped_mod(mod_id, app_config, conn)


def test_fix_rezipped_mod_raises_when_already_loadable(app_config, conn):
    mod_id = _install_real_mod(app_config, conn)

    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.fix_rezipped_mod(mod_id, app_config, conn)


def test_fix_rezipped_mod_raises_for_unknown_mod(app_config, conn):
    with pytest.raises(broken_mods.BrokenModFixError):
        broken_mods.fix_rezipped_mod("does-not-exist", app_config, conn)
