from pathlib import Path

import pytest

from backend import loose_mods
from backend import mod_manager


def _drop_loose_file(config, name: str, content: bytes = b"data") -> Path:
    path = config.sims4_mods_dir / name
    path.write_bytes(content)
    return path


# --- import_loose_files() ------------------------------------------------------------


def test_import_loose_files_adopts_a_loose_package(app_config, conn):
    _drop_loose_file(app_config, "SomeMod.package")

    imported = loose_mods.import_loose_files(app_config, conn)

    assert len(imported) == 1
    mod_id = imported[0]
    row = conn.execute("SELECT name, is_loose_import, active FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["name"] == "SomeMod"
    assert row["is_loose_import"] == 1
    assert row["active"] == 1


def test_import_loose_files_removes_the_original_file(app_config, conn):
    original = _drop_loose_file(app_config, "SomeMod.package")

    loose_mods.import_loose_files(app_config, conn)

    assert not original.exists()


def test_import_loose_files_creates_a_managed_symlink(app_config, conn):
    _drop_loose_file(app_config, "SomeMod.package")

    imported = loose_mods.import_loose_files(app_config, conn)

    link = app_config.sims4_mods_dir / imported[0]
    assert link.is_symlink()
    assert (link / "SomeMod.package").is_file()


def test_import_loose_files_adopts_loose_ts4script_too(app_config, conn):
    _drop_loose_file(app_config, "SomeScript.ts4script")

    imported = loose_mods.import_loose_files(app_config, conn)

    assert len(imported) == 1


def test_import_loose_files_ignores_non_mod_files(app_config, conn):
    _drop_loose_file(app_config, "readme.txt")
    _drop_loose_file(app_config, "screenshot.png")

    imported = loose_mods.import_loose_files(app_config, conn)

    assert imported == []
    assert (app_config.sims4_mods_dir / "readme.txt").exists()
    assert (app_config.sims4_mods_dir / "screenshot.png").exists()


def test_import_loose_files_ignores_directories_and_symlinks(app_config, conn, tmp_path):
    (app_config.sims4_mods_dir / "RealFolder").mkdir()
    (app_config.sims4_mods_dir / "RealFolder" / "inner.package").write_bytes(b"data")
    mod_id = mod_manager.install(
        _drop_loose_file(app_config, "temp-source.package"), config=app_config, conn=conn, mod_name="Managed"
    )
    (app_config.sims4_mods_dir / "temp-source.package").unlink(missing_ok=True)

    imported = loose_mods.import_loose_files(app_config, conn)

    assert imported == []  # RealFolder is a directory, the managed mod's entry is a symlink
    assert (app_config.sims4_mods_dir / "RealFolder").is_dir()


def test_import_loose_files_no_mods_dir_returns_empty(app_config):
    import shutil

    shutil.rmtree(app_config.sims4_mods_dir)

    assert loose_mods.import_loose_files(app_config, None) == []


def test_import_loose_files_imports_several_independently(app_config, conn):
    _drop_loose_file(app_config, "First.package")
    _drop_loose_file(app_config, "Second.package")

    imported = loose_mods.import_loose_files(app_config, conn)

    assert len(imported) == 2
    names = {conn.execute("SELECT name FROM mods WHERE id = ?", (mid,)).fetchone()["name"] for mid in imported}
    assert names == {"First", "Second"}


# --- suggest_groupings() --------------------------------------------------------------


def _import(app_config, conn, name) -> str:
    _drop_loose_file(app_config, f"{name}.package")
    return loose_mods.import_loose_files(app_config, conn)[0]


def test_suggest_groupings_clusters_shared_name_prefix(app_config, conn):
    for suffix in ["CandaceBra", "CandaceDress", "CandaceGarterBelt"]:
        _drop_loose_file(app_config, f"serenity_x_caio_{suffix}.package")
    loose_mods.import_loose_files(app_config, conn)

    suggestions = loose_mods.suggest_groupings(conn)

    assert len(suggestions) == 1
    assert len(suggestions[0].mod_ids) == 3
    assert "serenity" in suggestions[0].suggested_name.lower()


def test_suggest_groupings_ignores_mods_not_tagged_loose(app_config, conn, tmp_path):
    import zipfile

    archive = tmp_path / "Normal.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("normal_a.package", b"a")
    mod_manager.install(archive, config=app_config, conn=conn, mod_name="normal_a")
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("normal_b.package", b"b")
    mod_manager.install(archive, config=app_config, conn=conn, mod_name="normal_b")

    assert loose_mods.suggest_groupings(conn) == []


def test_suggest_groupings_no_group_for_a_single_unrelated_mod(app_config, conn):
    _import(app_config, conn, "TotallyUniqueNameHere")

    assert loose_mods.suggest_groupings(conn) == []


def test_suggest_groupings_does_not_double_count_a_mod_in_two_groups(app_config, conn):
    for suffix in ["ItemA", "ItemB"]:
        _drop_loose_file(app_config, f"creator_pack_{suffix}.package")
    loose_mods.import_loose_files(app_config, conn)

    suggestions = loose_mods.suggest_groupings(conn)

    all_ids = [mod_id for s in suggestions for mod_id in s.mod_ids]
    assert len(all_ids) == len(set(all_ids))


def test_suggest_groupings_clusters_shared_curseforge_id(app_config, conn):
    id_a = _import(app_config, conn, "TotallyUnrelatedNameOne")
    id_b = _import(app_config, conn, "CompletelyDifferentNameTwo")
    conn.execute("UPDATE mods SET curseforge_id = 12345 WHERE id IN (?, ?)", (id_a, id_b))
    conn.commit()

    suggestions = loose_mods.suggest_groupings(conn)

    assert len(suggestions) == 1
    assert suggestions[0].curseforge_id == 12345
    assert set(suggestions[0].mod_ids) == {id_a, id_b}


def test_suggest_groupings_curseforge_group_is_not_also_offered_by_name(app_config, conn):
    # Same shared name prefix *and* a shared curseforge_id — should surface
    # once, as the stronger confirmed-identity signal, not twice.
    for suffix in ["ItemA", "ItemB"]:
        _drop_loose_file(app_config, f"creator_pack_{suffix}.package")
    ids = loose_mods.import_loose_files(app_config, conn)
    conn.execute("UPDATE mods SET curseforge_id = 999 WHERE id IN (?, ?)", tuple(ids))
    conn.commit()

    suggestions = loose_mods.suggest_groupings(conn)

    assert len(suggestions) == 1
    assert suggestions[0].curseforge_id == 999


def test_suggest_groupings_curseforge_groups_come_before_name_groups(app_config, conn):
    id_a = _import(app_config, conn, "TotallyUnrelatedNameOne")
    id_b = _import(app_config, conn, "CompletelyDifferentNameTwo")
    conn.execute("UPDATE mods SET curseforge_id = 12345 WHERE id IN (?, ?)", (id_a, id_b))
    conn.commit()
    for suffix in ["ItemA", "ItemB"]:
        _drop_loose_file(app_config, f"creator_pack_{suffix}.package")
    loose_mods.import_loose_files(app_config, conn)

    suggestions = loose_mods.suggest_groupings(conn)

    assert len(suggestions) == 2
    assert suggestions[0].curseforge_id == 12345
    assert suggestions[1].curseforge_id is None


def test_suggest_groupings_a_lone_mod_with_a_curseforge_id_is_not_grouped(app_config, conn):
    id_a = _import(app_config, conn, "SoloLinkedMod")
    conn.execute("UPDATE mods SET curseforge_id = 42 WHERE id = ?", (id_a,))
    conn.commit()

    assert loose_mods.suggest_groupings(conn) == []


# --- merge_mods() ----------------------------------------------------------------------


def test_merge_mods_combines_files_into_one_new_mod(app_config, conn):
    id_a = _import(app_config, conn, "creator_pack_ItemA")
    id_b = _import(app_config, conn, "creator_pack_ItemB")

    new_id = loose_mods.merge_mods([id_a, id_b], "Creator Pack", config=app_config, conn=conn)

    row = conn.execute("SELECT name, is_loose_import FROM mods WHERE id = ?", (new_id,)).fetchone()
    assert row["name"] == "Creator Pack"
    assert row["is_loose_import"] == 0
    library_path = Path(conn.execute("SELECT library_path FROM mods WHERE id = ?", (new_id,)).fetchone()["library_path"])
    assert (library_path / "creator_pack_ItemA.package").is_file()
    assert (library_path / "creator_pack_ItemB.package").is_file()


def test_merge_mods_removes_the_old_mods(app_config, conn):
    id_a = _import(app_config, conn, "creator_pack_ItemA")
    id_b = _import(app_config, conn, "creator_pack_ItemB")

    loose_mods.merge_mods([id_a, id_b], "Creator Pack", config=app_config, conn=conn)

    assert conn.execute("SELECT 1 FROM mods WHERE id = ?", (id_a,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM mods WHERE id = ?", (id_b,)).fetchone() is None


def test_merge_mods_backs_up_each_source_folder(app_config, conn, monkeypatch):
    import datetime as dt_module

    class _FakeClock:
        _seconds = 0

        @classmethod
        def now(cls, tz):
            cls._seconds += 1
            return dt_module.datetime(2026, 1, 1, 0, 0, cls._seconds % 60, tzinfo=tz)

    from backend import backups as backups_module

    monkeypatch.setattr(backups_module, "datetime", _FakeClock)
    id_a = _import(app_config, conn, "creator_pack_ItemA")
    id_b = _import(app_config, conn, "creator_pack_ItemB")

    loose_mods.merge_mods([id_a, id_b], "Creator Pack", config=app_config, conn=conn)

    backups_dir = app_config.library_dir / ".backups"
    assert list(backups_dir.glob(f"{id_a}-*"))
    assert list(backups_dir.glob(f"{id_b}-*"))


def test_merge_mods_disambiguates_filename_collisions(app_config, conn):
    _drop_loose_file(app_config, "one.package")
    id_a = loose_mods.import_loose_files(app_config, conn)[0]
    # Second mod happens to share the exact same relative filename as the
    # first once installed.
    _drop_loose_file(app_config, "two.package")
    id_b = loose_mods.import_loose_files(app_config, conn)[0]
    library_b = Path(conn.execute("SELECT library_path FROM mods WHERE id = ?", (id_b,)).fetchone()["library_path"])
    (library_b / "two.package").rename(library_b / "one.package")
    conn.execute(
        "UPDATE mod_files SET relative_path = 'one.package' WHERE mod_id = ?", (id_b,)
    )
    conn.commit()

    new_id = loose_mods.merge_mods([id_a, id_b], "Merged", config=app_config, conn=conn)

    library_path = Path(conn.execute("SELECT library_path FROM mods WHERE id = ?", (new_id,)).fetchone()["library_path"])
    assert len(list(library_path.glob("*.package"))) == 2


def test_merge_mods_rejects_fewer_than_two_ids(app_config, conn):
    id_a = _import(app_config, conn, "SoloMod")

    with pytest.raises(loose_mods.LooseModsError):
        loose_mods.merge_mods([id_a], "Solo", config=app_config, conn=conn)


def test_merge_mods_rejects_unknown_mod_id(app_config, conn):
    id_a = _import(app_config, conn, "SoloMod")

    with pytest.raises(loose_mods.LooseModsError):
        loose_mods.merge_mods([id_a, "does-not-exist"], "Combined", config=app_config, conn=conn)


def test_merge_mods_carries_over_shared_curseforge_metadata(app_config, conn):
    id_a = _import(app_config, conn, "TotallyUnrelatedNameOne")
    id_b = _import(app_config, conn, "CompletelyDifferentNameTwo")
    conn.execute(
        "UPDATE mods SET curseforge_id = 12345, author = 'RealAuthor', category = 'Build', "
        "short_description = 'The real thing.', thumbnail_url = 'https://x/y.png', "
        "links = '{\"curseforge_url\": \"https://x\"}' WHERE id IN (?, ?)",
        (id_a, id_b),
    )
    conn.commit()

    new_id = loose_mods.merge_mods([id_a, id_b], "Merged Mod", config=app_config, conn=conn)

    row = conn.execute(
        "SELECT curseforge_id, author, category, short_description, thumbnail_url, links "
        "FROM mods WHERE id = ?",
        (new_id,),
    ).fetchone()
    assert row["curseforge_id"] == 12345
    assert row["author"] == "RealAuthor"
    assert row["category"] == "Build"
    assert row["short_description"] == "The real thing."
    assert row["thumbnail_url"] == "https://x/y.png"
    assert row["links"] is not None


def test_merge_mods_does_not_invent_curseforge_metadata_for_a_name_only_group(app_config, conn):
    id_a = _import(app_config, conn, "creator_pack_ItemA")
    id_b = _import(app_config, conn, "creator_pack_ItemB")

    new_id = loose_mods.merge_mods([id_a, id_b], "Creator Pack", config=app_config, conn=conn)

    row = conn.execute("SELECT curseforge_id, author FROM mods WHERE id = ?", (new_id,)).fetchone()
    assert row["curseforge_id"] is None
    assert row["author"] is None


def test_merge_mods_does_not_carry_metadata_when_curseforge_ids_disagree(app_config, conn):
    id_a = _import(app_config, conn, "TotallyUnrelatedNameOne")
    id_b = _import(app_config, conn, "CompletelyDifferentNameTwo")
    conn.execute("UPDATE mods SET curseforge_id = 111, author = 'AuthorOne' WHERE id = ?", (id_a,))
    conn.execute("UPDATE mods SET curseforge_id = 222, author = 'AuthorTwo' WHERE id = ?", (id_b,))
    conn.commit()

    new_id = loose_mods.merge_mods([id_a, id_b], "Merged Mod", config=app_config, conn=conn)

    row = conn.execute("SELECT curseforge_id, author FROM mods WHERE id = ?", (new_id,)).fetchone()
    assert row["curseforge_id"] is None
    assert row["author"] is None


def test_merge_mods_rejects_a_mod_not_tagged_loose(app_config, conn, tmp_path):
    import zipfile

    id_a = _import(app_config, conn, "SoloMod")
    archive = tmp_path / "Normal.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("normal.package", b"data")
    normal_id = mod_manager.install(archive, config=app_config, conn=conn, mod_name="NormalMod")

    with pytest.raises(loose_mods.LooseModsError):
        loose_mods.merge_mods([id_a, normal_id], "Combined", config=app_config, conn=conn)
