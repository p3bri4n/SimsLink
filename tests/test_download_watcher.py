import threading
import time
import zipfile
from pathlib import Path

import pytest

import download_watcher
import mod_manager


def _install_mod(app_config, conn, tmp_path, name="Cool Mod", filename="mymod.package") -> str:
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename, b"data")
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name)


# --- watcher detection (mocked filesystem events, no network, no real Downloads dir) --
# The watcher itself never touches sqlite3 (cross-thread connections aren't
# safe) — it only reports paths; matching happens separately, on whichever
# thread owns `conn`.


def test_watcher_detects_new_zip_after_settling(app_config):
    detected: list[Path] = []
    fired = threading.Event()

    def on_detected(path: Path) -> None:
        detected.append(path)
        fired.set()

    watcher = download_watcher.DownloadWatcher(app_config, on_detected, settle_seconds=0.1)
    watcher.start()
    try:
        time.sleep(0.2)  # let the observer thread finish its initial setup
        target = app_config.download_watch_dir / "NewMod.zip"
        target.write_bytes(b"zip-bytes")
        assert fired.wait(timeout=5), "watcher did not report the new file"
    finally:
        watcher.stop()

    assert detected == [target]


def test_watcher_ignores_unrelated_extensions(app_config):
    fired = threading.Event()
    watcher = download_watcher.DownloadWatcher(app_config, lambda p: fired.set(), settle_seconds=0.1)
    watcher.start()
    try:
        time.sleep(0.2)
        (app_config.download_watch_dir / "readme.txt").write_text("hi")
        assert not fired.wait(timeout=0.5)
    finally:
        watcher.stop()


def test_watcher_debounces_repeated_writes_to_one_report(app_config):
    detected: list[Path] = []
    watcher = download_watcher.DownloadWatcher(app_config, detected.append, settle_seconds=0.2)
    watcher.start()
    try:
        time.sleep(0.2)
        target = app_config.download_watch_dir / "Chunked.zip"
        for _ in range(3):
            with target.open("ab") as f:
                f.write(b"chunk")
            time.sleep(0.05)
        time.sleep(1.0)  # well past settle_seconds since the last write
    finally:
        watcher.stop()

    assert len(detected) == 1


# --- filename-proximity matching (runs on the caller's own thread) ---------


def test_match_existing_mod_finds_close_filename(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, name="Cool Mod")

    candidate_id, candidate_name = download_watcher.match_existing_mod(
        Path("Cool-Mod-v2.zip"), conn
    )

    assert candidate_id == mod_id
    assert candidate_name == "Cool Mod"


def test_match_existing_mod_none_for_dissimilar_filename(app_config, conn, tmp_path):
    _install_mod(app_config, conn, tmp_path, name="Cool Mod")

    candidate_id, candidate_name = download_watcher.match_existing_mod(
        Path("totally-unrelated-name.zip"), conn
    )

    assert candidate_id is None
    assert candidate_name is None


def test_match_existing_mod_none_when_no_mods_installed(app_config, conn):
    candidate_id, candidate_name = download_watcher.match_existing_mod(Path("anything.zip"), conn)

    assert candidate_id is None
    assert candidate_name is None


# --- confirm_install / confirm_replace --------------------------------------


def test_confirm_install_installs_new_mod(app_config, conn, tmp_path):
    archive = tmp_path / "New.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("new.package", b"data")

    mod_id = download_watcher.confirm_install(archive, config=app_config, conn=conn, mod_name="New Mod")

    row = conn.execute("SELECT name FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["name"] == "New Mod"


def test_confirm_replace_backs_up_old_version_and_reinstalls(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, name="Cool Mod", filename="old.package")

    archive = tmp_path / "CoolModV2.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("new.package", b"new-data")

    new_mod_id = download_watcher.confirm_replace(archive, mod_id, config=app_config, conn=conn)

    assert new_mod_id == mod_id  # same name -> same slug, freed up by the delete
    backups = list((app_config.library_dir / ".backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "old.package").is_file()
    assert (app_config.library_dir / mod_id / "new.package").is_file()
    assert not (app_config.library_dir / mod_id / "old.package").exists()


def test_confirm_replace_unknown_mod_raises(app_config, conn, tmp_path):
    archive = tmp_path / "New.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("new.package", b"data")

    with pytest.raises(download_watcher.DownloadWatcherError):
        download_watcher.confirm_replace(archive, "does-not-exist", config=app_config, conn=conn)


def test_confirm_replace_carries_metadata_through(app_config, conn, tmp_path):
    """Direct Mode's ui/updates.py passes fresh CurseForge metadata into a
    replace so the updated row doesn't lose curseforge_id/compat_status —
    without this the replace path (delete + plain install) would silently
    drop it, since mod_manager.install() defaults metadata to empty."""
    mod_id = _install_mod(app_config, conn, tmp_path, name="Cool Mod", filename="old.package")

    archive = tmp_path / "CoolModV2.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("new.package", b"new-data")
    metadata = mod_manager.ModMetadata(
        curseforge_id=111, installed_version="222", compat_status="compatible"
    )

    new_mod_id = download_watcher.confirm_replace(
        archive, mod_id, config=app_config, conn=conn, metadata=metadata
    )

    row = conn.execute("SELECT * FROM mods WHERE id = ?", (new_mod_id,)).fetchone()
    assert row["curseforge_id"] == 111
    assert row["installed_version"] == "222"
    assert row["compat_status"] == "compatible"
