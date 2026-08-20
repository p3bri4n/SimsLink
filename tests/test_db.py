import sqlite3

import pytest

import db


EXPECTED_TABLES = {
    "mods",
    "mod_files",
    "dependencies",
    "profiles",
    "profile_mods",
    "crash_log",
}


def test_init_db_creates_all_tables(tmp_path):
    conn = db.init_db(tmp_path / "simslink.sqlite3")

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    assert EXPECTED_TABLES <= tables


def test_init_db_sets_user_version_to_latest(tmp_path):
    conn = db.init_db(tmp_path / "simslink.sqlite3")

    version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert version == db.LATEST_VERSION


def test_migrate_is_idempotent(tmp_path):
    db_path = tmp_path / "simslink.sqlite3"

    conn = db.init_db(db_path)
    conn.close()

    # Reopening and migrating again must not error or re-run applied scripts.
    conn = db.init_db(db_path)

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.LATEST_VERSION


def test_foreign_keys_are_enforced(tmp_path):
    conn = db.init_db(tmp_path / "simslink.sqlite3")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO mod_files (mod_id, relative_path, extension) "
            "VALUES ('does-not-exist', 'Scripts/foo.ts4script', 'ts4script')"
        )


def test_mods_primary_type_check_constraint(tmp_path):
    conn = db.init_db(tmp_path / "simslink.sqlite3")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO mods (id, name, library_path, primary_type, install_date) "
            "VALUES ('some-mod', 'Some Mod', '/lib/some-mod', 'not-a-real-type', '2026-08-20')"
        )


def test_mod_files_cascade_delete(tmp_path):
    conn = db.init_db(tmp_path / "simslink.sqlite3")
    conn.execute(
        "INSERT INTO mods (id, name, library_path, primary_type, install_date) "
        "VALUES ('some-mod', 'Some Mod', '/lib/some-mod', 'package', '2026-08-20')"
    )
    conn.execute(
        "INSERT INTO mod_files (mod_id, relative_path, extension) "
        "VALUES ('some-mod', 'file.package', 'package')"
    )
    conn.commit()

    conn.execute("DELETE FROM mods WHERE id = 'some-mod'")
    conn.commit()

    remaining = conn.execute("SELECT COUNT(*) FROM mod_files").fetchone()[0]
    assert remaining == 0
