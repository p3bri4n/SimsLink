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
    assert groups[0].file_count == 1
    assert sorted(groups[0].mod_ids) == sorted([mod_a, mod_b])


def test_regression_multiple_shared_files_between_same_pair_collapse_to_one_group(app_config, conn, tmp_path):
    # Two mods sharing many identical files (a real, observed case — e.g. an
    # "override" mod re-bundling most of another mod's files) used to
    # produce one ConflictGroup per shared hash: dozens of near-identical
    # rows differing only by an opaque hash the UI never showed, reading as
    # a rendering bug rather than a meaningful signal. Now: one group per
    # mod pair, with file_count reporting how many files they share.
    mod_a = _install(
        tmp_path, app_config, conn, "Mod A",
        {"one.package": b"aaa", "two.package": b"bbb", "three.package": b"ccc"},
    )
    mod_b = _install(
        tmp_path, app_config, conn, "Mod B",
        {"one.package": b"aaa", "two.package": b"bbb", "three.package": b"ccc"},
    )

    groups = cd.find_package_duplicates(conn)

    assert len(groups) == 1
    assert groups[0].file_count == 3
    assert sorted(groups[0].mod_ids) == sorted([mod_a, mod_b])


def test_regression_disabling_one_of_three_still_reports_remaining_active_pair(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "Mod A", {"shared.package": b"same-bytes"})
    mod_b = _install(tmp_path, app_config, conn, "Mod B", {"shared.package": b"same-bytes"})
    mod_c = _install(tmp_path, app_config, conn, "Mod C", {"shared.package": b"same-bytes"})
    mod_manager.disable(mod_a, config=app_config, conn=conn)

    groups = cd.find_package_duplicates(conn)

    assert len(groups) == 1
    assert sorted(groups[0].mod_ids) == sorted([mod_b, mod_c])


def test_find_package_duplicates_keeps_different_mod_pairs_separate(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "Mod A", {"shared1.package": b"aaa"})
    mod_b = _install(tmp_path, app_config, conn, "Mod B", {"shared1.package": b"aaa"})
    mod_c = _install(tmp_path, app_config, conn, "Mod C", {"shared2.package": b"zzz"})
    mod_d = _install(tmp_path, app_config, conn, "Mod D", {"shared2.package": b"zzz"})

    groups = cd.find_package_duplicates(conn)

    assert len(groups) == 2
    pairs = {tuple(sorted(g.mod_ids)) for g in groups}
    assert pairs == {tuple(sorted([mod_a, mod_b])), tuple(sorted([mod_c, mod_d]))}


def test_regression_disabled_mod_does_not_count_as_duplicate_conflict(app_config, conn, tmp_path):
    # A disabled mod's files aren't loaded by the game, so it can't actually
    # collide with anything right now — disabling one side of a conflict is
    # itself a valid resolution and should make the conflict disappear
    # entirely, not just stop mentioning the disabled mod.
    mod_a = _install(tmp_path, app_config, conn, "Mod A", {"shared.package": b"same-bytes"})
    _install(tmp_path, app_config, conn, "Mod B", {"shared.package": b"same-bytes"})
    mod_manager.disable(mod_a, config=app_config, conn=conn)

    assert cd.find_package_duplicates(conn) == []


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


def test_regression_disabled_mod_does_not_count_as_ts4script_collision(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "Mod A", {"lib/core.ts4script": b"content-a"})
    _install(tmp_path, app_config, conn, "Mod B", {"core.ts4script": b"content-b-different"})
    mod_manager.disable(mod_a, config=app_config, conn=conn)

    assert cd.find_ts4script_name_collisions(conn) == []


def test_find_ts4script_name_collisions_ignores_unique_names(app_config, conn, tmp_path):
    _install(tmp_path, app_config, conn, "Mod A", {"a.ts4script": b"data-a"})
    _install(tmp_path, app_config, conn, "Mod B", {"b.ts4script": b"data-b"})

    assert cd.find_ts4script_name_collisions(conn) == []


def test_find_ts4script_name_collisions_ignores_package_files(app_config, conn, tmp_path):
    _install(tmp_path, app_config, conn, "Mod A", {"shared.package": b"data"})
    _install(tmp_path, app_config, conn, "Mod B", {"shared.package": b"data"})

    assert cd.find_ts4script_name_collisions(conn) == []


# --- folder duplication (name suffix) detection ----------------------------------


def test_find_folder_duplications_detects_numeric_suffix_pattern(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "SimNationTravel_v6.9.4", {"a.package": b"a"})
    mod_b = _install(tmp_path, app_config, conn, "SimNationTravel_v6.9.4(1)", {"b.package": b"b"})

    groups = cd.find_folder_duplications(conn)

    assert len(groups) == 1
    assert groups[0].kind == "folder_duplication"
    assert groups[0].identifier == "SimNationTravel_v6.9.4"
    assert sorted(groups[0].mod_ids) == sorted([mod_a, mod_b])


def test_find_folder_duplications_handles_three_way_and_space_before_parens(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "Cool Mod", {"a.package": b"a"})
    mod_b = _install(tmp_path, app_config, conn, "Cool Mod (1)", {"b.package": b"b"})
    mod_c = _install(tmp_path, app_config, conn, "Cool Mod (2)", {"c.package": b"c"})

    groups = cd.find_folder_duplications(conn)

    assert len(groups) == 1
    assert sorted(groups[0].mod_ids) == sorted([mod_a, mod_b, mod_c])


def test_find_folder_duplications_ignores_identically_named_mods_without_a_suffix(app_config, conn, tmp_path):
    # Two mods that just happen to share a name isn't the "downloaded twice"
    # pattern this detects — no (N) suffix was ever stripped from either.
    _install(tmp_path, app_config, conn, "Same Name", {"a.package": b"a"})
    _install(tmp_path, app_config, conn, "Same Name", {"b.package": b"b"})

    assert cd.find_folder_duplications(conn) == []


def test_find_folder_duplications_ignores_unrelated_names(app_config, conn, tmp_path):
    _install(tmp_path, app_config, conn, "Mod A", {"a.package": b"a"})
    _install(tmp_path, app_config, conn, "Mod A (1)", {"b.package": b"b"})
    _install(tmp_path, app_config, conn, "Mod B", {"c.package": b"c"})

    groups = cd.find_folder_duplications(conn)

    assert len(groups) == 1
    assert groups[0].identifier == "Mod A"


def test_regression_disabled_mod_excluded_from_folder_duplication(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "Cool Mod", {"a.package": b"a"})
    _install(tmp_path, app_config, conn, "Cool Mod (1)", {"b.package": b"b"})
    mod_manager.disable(mod_a, config=app_config, conn=conn)

    assert cd.find_folder_duplications(conn) == []


# --- combined -------------------------------------------------------------------


def test_find_conflicts_combines_both_kinds(app_config, conn, tmp_path):
    _install(tmp_path, app_config, conn, "Mod A", {"shared.package": b"same-bytes", "core.ts4script": b"x"})
    _install(tmp_path, app_config, conn, "Mod B", {"shared.package": b"same-bytes", "core.ts4script": b"y"})

    groups = cd.find_conflicts(conn)

    assert {g.kind for g in groups} == {"duplicate_package", "ts4script_name_collision"}


def test_regression_folder_duplication_suppresses_generic_signals_for_same_pair(app_config, conn, tmp_path):
    # Real observed case: a pair whose names differ only by "(1)" also shared
    # an identical .package and a same-named .ts4script — reporting all three
    # signals for the same underlying issue read as noise/a bug, not three
    # separate findings. The specific, high-confidence signal wins; the two
    # generic ones are suppressed for this pair.
    _install(
        tmp_path, app_config, conn, "SimNationTravel_v6.9.4",
        {"shared.package": b"same-bytes", "core.ts4script": b"x"},
    )
    _install(
        tmp_path, app_config, conn, "SimNationTravel_v6.9.4(1)",
        {"shared.package": b"same-bytes", "core.ts4script": b"y"},
    )

    groups = cd.find_conflicts(conn)

    assert [g.kind for g in groups] == ["folder_duplication"]


def test_find_conflicts_folder_duplication_does_not_suppress_unrelated_pairs(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "Renamed Mod", {"shared.package": b"same-bytes"})
    mod_b = _install(tmp_path, app_config, conn, "Renamed Mod (1)", {"other.package": b"z"})
    # mod_c also has a file of its own so its full file set doesn't happen to
    # exactly match mod_a's (that's find_exact_duplicate_mods()'s signal,
    # tested separately below) — this test is specifically about
    # duplicate_package's single-shared-file signal surviving suppression.
    mod_c = _install(
        tmp_path, app_config, conn, "Totally Different Mod",
        {"shared.package": b"same-bytes", "extra.package": b"only-in-c"},
    )

    groups = cd.find_conflicts(conn)

    kinds = {g.kind for g in groups}
    assert "folder_duplication" in kinds
    # mod_a/mod_c's shared file isn't covered by the folder_duplication pair
    # (mod_a, mod_b), so it must still be reported.
    duplicate_groups = [g for g in groups if g.kind == "duplicate_package"]
    assert len(duplicate_groups) == 1
    assert sorted(duplicate_groups[0].mod_ids) == sorted([mod_a, mod_c])


def test_find_conflicts_empty_library_returns_empty(conn):
    assert cd.find_conflicts(conn) == []


# --- exact duplicate mods (100% shared file set) -----------------------------


def test_find_exact_duplicate_mods_detects_identical_file_sets(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "Mod A", {"one.package": b"aaa", "two.package": b"bbb"})
    mod_b = _install(tmp_path, app_config, conn, "Mod B", {"one.package": b"aaa", "two.package": b"bbb"})

    groups = cd.find_exact_duplicate_mods(conn)

    assert len(groups) == 1
    assert groups[0].kind == "exact_duplicate_mod"
    assert groups[0].file_count == 2
    assert sorted(groups[0].mod_ids) == sorted([mod_a, mod_b])


def test_find_exact_duplicate_mods_ignores_partial_overlap(app_config, conn, tmp_path):
    # mod_b has every file mod_a has, plus one more of its own — not a 100%
    # match in either direction once you account for the extra file, so
    # this stays a plain duplicate_package case, not an exact duplicate.
    _install(tmp_path, app_config, conn, "Mod A", {"one.package": b"aaa"})
    _install(tmp_path, app_config, conn, "Mod B", {"one.package": b"aaa", "two.package": b"bbb"})

    assert cd.find_exact_duplicate_mods(conn) == []


def test_find_exact_duplicate_mods_ignores_disabled_mods(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "Mod A", {"one.package": b"aaa"})
    _install(tmp_path, app_config, conn, "Mod B", {"one.package": b"aaa"})
    mod_manager.disable(mod_a, config=app_config, conn=conn)

    assert cd.find_exact_duplicate_mods(conn) == []


def test_find_exact_duplicate_mods_ignores_unrelated_mods(app_config, conn, tmp_path):
    _install(tmp_path, app_config, conn, "Mod A", {"one.package": b"aaa"})
    _install(tmp_path, app_config, conn, "Mod B", {"two.package": b"bbb"})

    assert cd.find_exact_duplicate_mods(conn) == []


def test_find_conflicts_exact_duplicate_suppresses_weaker_signals_for_same_pair(app_config, conn, tmp_path):
    # A pair that's a 100% file match is, by definition, also caught by
    # duplicate_package (they share every file) — only the strongest
    # signal should be reported for it.
    mod_a = _install(tmp_path, app_config, conn, "Mod A", {"one.package": b"aaa", "two.package": b"bbb"})
    mod_b = _install(tmp_path, app_config, conn, "Mod B", {"one.package": b"aaa", "two.package": b"bbb"})

    groups = cd.find_conflicts(conn)

    assert [g.kind for g in groups] == ["exact_duplicate_mod"]
    assert sorted(groups[0].mod_ids) == sorted([mod_a, mod_b])


def test_find_conflicts_exact_duplicate_does_not_suppress_unrelated_pairs(app_config, conn, tmp_path):
    mod_a = _install(tmp_path, app_config, conn, "Mod A", {"one.package": b"aaa"})
    mod_b = _install(tmp_path, app_config, conn, "Mod B", {"one.package": b"aaa"})
    mod_c = _install(
        tmp_path, app_config, conn, "Mod C", {"one.package": b"aaa", "unique.package": b"only-in-c"}
    )

    groups = cd.find_conflicts(conn)

    kinds = {g.kind for g in groups}
    assert "exact_duplicate_mod" in kinds
    assert "duplicate_package" in kinds
    duplicate_groups = [g for g in groups if g.kind == "duplicate_package"]
    assert len(duplicate_groups) == 1
    assert mod_c in duplicate_groups[0].mod_ids
    assert mod_a in duplicate_groups[0].mod_ids or mod_b in duplicate_groups[0].mod_ids
