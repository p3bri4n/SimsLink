import zipfile
from pathlib import Path

from backend import conflict_detector as cd
from backend import mod_manager


def _install(tmp_path: Path, app_config, conn, name: str, files: dict[str, bytes]) -> str:
    archive = tmp_path / f"{name}-src.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for rel_path, content in files.items():
            zf.writestr(rel_path, content)
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name)


# --- .package duplicate detection --------------------------------------------


def test_find_package_duplicates_detects_identical_files_across_mods(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "Mod A", {"shared.package": b"same-bytes"})
    mod_b = _install(tmp_path, app_config, conn, "Mod B", {"shared.package": b"same-bytes"})

    groups = cd.find_package_duplicates(conn)

    assert len(groups) == 1
    assert groups[0].kind == "duplicate_package"
    assert sorted(groups[0].mod_ids) == sorted([mod_a, mod_b])


def test_find_package_duplicates_ignores_files_unique_to_one_mod(app_config, conn, tmp_path):
    _install(tmp_path, app_config, conn, "Mod A", {"a.package": b"data-a"})
    _install(tmp_path, app_config, conn, "Mod B", {"b.package": b"data-b"})

    assert cd.find_package_duplicates(conn) == []


def test_find_package_duplicates_ignores_duplicate_within_the_same_mod(app_config, conn, tmp_path):
    # Two identical files inside the same mod (e.g. a re-exported resource)
    # isn't a cross-mod conflict — nothing for the user to reconcile.
    _install(
        tmp_path, app_config, conn, "Mod A",
        {"one.package": b"same-bytes", "sub/two.package": b"same-bytes"},
    )

    assert cd.find_package_duplicates(conn) == []


def test_find_package_duplicates_ignores_ts4script_files(app_config, conn, tmp_path):
    _install(tmp_path, app_config, conn, "Mod A", {"core.ts4script": b"same-bytes"})
    _install(tmp_path, app_config, conn, "Mod B", {"core.ts4script": b"same-bytes"})

    assert cd.find_package_duplicates(conn) == []


# --- .ts4script name-collision detection ----------------------------------------


def test_find_ts4script_name_collisions_detects_same_filename_across_mods(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "Mod A", {"lib/core.ts4script": b"content-a"})
    mod_b = _install(tmp_path, app_config, conn, "Mod B", {"core.ts4script": b"content-b-different"})

    groups = cd.find_ts4script_name_collisions(conn)

    assert len(groups) == 1
    assert groups[0].kind == "ts4script_name_collision"
    assert groups[0].identifier == "core.ts4script"
    assert sorted(groups[0].mod_ids) == sorted([mod_a, mod_b])


def test_find_ts4script_name_collisions_ignores_unique_names(app_config, conn, tmp_path):
    _install(tmp_path, app_config, conn, "Mod A", {"a.ts4script": b"data-a"})
    _install(tmp_path, app_config, conn, "Mod B", {"b.ts4script": b"data-b"})

    assert cd.find_ts4script_name_collisions(conn) == []


def test_find_ts4script_name_collisions_ignores_package_files(app_config, conn, tmp_path):
    _install(tmp_path, app_config, conn, "Mod A", {"shared.package": b"data"})
    _install(tmp_path, app_config, conn, "Mod B", {"shared.package": b"data"})

    assert cd.find_ts4script_name_collisions(conn) == []


# --- combined -------------------------------------------------------------------


def test_find_conflicts_combines_both_kinds(app_config, conn, tmp_path):
    _install(tmp_path, app_config, conn, "Mod A", {"shared.package": b"same-bytes", "core.ts4script": b"x"})
    _install(tmp_path, app_config, conn, "Mod B", {"shared.package": b"same-bytes", "core.ts4script": b"y"})

    groups = cd.find_conflicts(conn)

    assert {g.kind for g in groups} == {"duplicate_package", "ts4script_name_collision"}


def test_find_conflicts_empty_library_returns_empty(conn):
    assert cd.find_conflicts(conn) == []
