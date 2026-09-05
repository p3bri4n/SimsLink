import os
import threading
import time
import zipfile
from pathlib import Path

from backend import mod_manager
from backend import scanner


def _install_one_file_mod(app_config, conn, tmp_path, filename="mymod.package", content=b"data"):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename, content)
    return mod_manager.install(archive, config=app_config, conn=conn)


# --- incremental scan (CLAUDE.md priority coverage) -------------------------


def test_incremental_scan_skips_unchanged_files(app_config, conn, tmp_path):
    _install_one_file_mod(app_config, conn, tmp_path)

    # install() already hashed the file once; scanning untouched files again
    # must compute zero new hashes.
    stats = scanner.incremental_scan(app_config, conn)

    assert stats.files_hashed == 0
    assert stats.files_unchanged == 1


def test_incremental_scan_rehashes_changed_file(app_config, conn, tmp_path):
    mod_id = _install_one_file_mod(app_config, conn, tmp_path)
    target = app_config.library_dir / mod_id / "mymod.package"
    target.write_bytes(b"different-content-now")
    bumped_mtime = target.stat().st_mtime + 1
    os.utime(target, (bumped_mtime, bumped_mtime))

    stats = scanner.incremental_scan(app_config, conn)

    assert stats.files_hashed == 1
    assert stats.files_unchanged == 0
    row = conn.execute(
        "SELECT hash FROM mod_files WHERE mod_id = ? AND relative_path = ?",
        (mod_id, "mymod.package"),
    ).fetchone()
    assert row["hash"] == mod_manager.hash_file(target)


def test_incremental_scan_removes_deleted_files(app_config, conn, tmp_path):
    mod_id = _install_one_file_mod(app_config, conn, tmp_path)
    (app_config.library_dir / mod_id / "mymod.package").unlink()

    stats = scanner.incremental_scan(app_config, conn)

    assert stats.files_removed == 1
    remaining = conn.execute(
        "SELECT COUNT(*) FROM mod_files WHERE mod_id = ?", (mod_id,)
    ).fetchone()[0]
    assert remaining == 0


def test_incremental_scan_picks_up_new_file_added_externally(app_config, conn, tmp_path):
    mod_id = _install_one_file_mod(app_config, conn, tmp_path)
    (app_config.library_dir / mod_id / "extra.package").write_bytes(b"extra")

    stats = scanner.incremental_scan(app_config, conn)

    assert stats.files_hashed == 1
    assert stats.files_unchanged == 1


# --- full scan ---------------------------------------------------------------


def test_full_scan_rehashes_every_file_even_if_unchanged(app_config, conn, tmp_path):
    _install_one_file_mod(app_config, conn, tmp_path)

    stats = scanner.full_scan(app_config, conn, max_workers=1)

    assert stats.files_hashed == 1


def test_full_scan_with_no_mods_returns_zero_stats(app_config, conn):
    stats = scanner.full_scan(app_config, conn)

    assert stats == scanner.ScanStats(mods_scanned=0)


# --- import of pre-existing unmanaged mods -----------------------------------


def test_import_untracked_mods_adopts_preexisting_folder(app_config, conn):
    preexisting = app_config.sims4_mods_dir / "SomeOldMod"
    preexisting.mkdir()
    (preexisting / "old.package").write_bytes(b"legacy-data")

    imported = scanner.import_untracked_mods(app_config, conn)

    assert len(imported) == 1
    mod_id = imported[0]
    link = app_config.sims4_mods_dir / mod_id
    assert link.is_symlink()
    row = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["active"] == 1


def test_import_untracked_mods_skips_already_managed_folders(app_config, conn, tmp_path):
    _install_one_file_mod(app_config, conn, tmp_path)

    imported = scanner.import_untracked_mods(app_config, conn)

    assert imported == []


def test_import_untracked_mods_ignores_loose_files_at_mods_root(app_config, conn):
    (app_config.sims4_mods_dir / "stray.package").write_bytes(b"x")

    imported = scanner.import_untracked_mods(app_config, conn)

    assert imported == []


def test_regression_unimportable_folder_does_not_abort_rest_of_scan(app_config, conn):
    # A folder under Mods/ with no .package/.ts4script anywhere in it (e.g.
    # an extracted .ts4script left as loose .pyc files) used to raise
    # ModManagerError uncaught, aborting the whole scan and leaving every
    # later entry (sorted alphabetically after it) unimported.
    unimportable = app_config.sims4_mods_dir / "AAA_ExtractedScript"
    unimportable.mkdir()
    (unimportable / "some_module.pyc").write_bytes(b"not tracked")

    importable = app_config.sims4_mods_dir / "ZZZ_RealMod"
    importable.mkdir()
    (importable / "real.package").write_bytes(b"data")

    imported = scanner.import_untracked_mods(app_config, conn)

    assert len(imported) == 1
    assert unimportable.is_dir() and not unimportable.is_symlink()


# --- real-time watcher ---------------------------------------------------


def test_mods_folder_watcher_fires_callback_on_change(app_config):
    fired = threading.Event()
    watcher = scanner.ModsFolderWatcher(app_config, on_change=fired.set)
    watcher.start()
    try:
        time.sleep(0.2)  # let the observer thread finish its initial setup
        (app_config.sims4_mods_dir / "newfile.package").write_bytes(b"x")
        assert fired.wait(timeout=5), "watcher did not fire on filesystem change"
    finally:
        watcher.stop()
