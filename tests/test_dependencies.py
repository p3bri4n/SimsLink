import zipfile
from pathlib import Path

import pytest

import dependencies as deps
import mod_manager
import package_parser as pp


def _install_mod(app_config, conn, tmp_path, name, filename="mymod.package", content=b"data"):
    archive = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename, content)
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name)


def _set_description(conn, mod_id, description):
    conn.execute("UPDATE mods SET full_description = ? WHERE id = ?", (description, mod_id))
    conn.commit()


def _set_links(conn, mod_id, links_text):
    conn.execute("UPDATE mods SET links = ? WHERE id = ?", (links_text, mod_id))
    conn.commit()


# --- required/optional resolution -------------------------------------------


def test_required_dependency_blocks_enable_when_unresolved(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Needs Core")
    mod_manager.disable(mod_id, config=app_config, conn=conn)
    core_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    mod_manager.disable(core_id, config=app_config, conn=conn)

    deps.add_dependency(
        mod_id, conn=conn, dependency_type="required", depends_on_mod_id=core_id
    )

    with pytest.raises(deps.UnresolvedRequiredDependencyError):
        mod_manager.enable(mod_id, config=app_config, conn=conn)


def test_required_dependency_allows_enable_once_resolved(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Needs Core")
    core_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    # core_id is active (fresh install), so the dependency is already resolved.
    deps.add_dependency(mod_id, conn=conn, dependency_type="required", depends_on_mod_id=core_id)

    mod_manager.enable(mod_id, config=app_config, conn=conn)

    row = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["active"] == 1


def test_optional_dependency_does_not_block_enable(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Needs Core Optionally")
    mod_manager.disable(mod_id, config=app_config, conn=conn)

    deps.add_dependency(
        mod_id,
        conn=conn,
        dependency_type="optional",
        depends_on_curseforge_id=99999,  # not installed, doesn't matter
        mandatory=False,
    )

    mod_manager.enable(mod_id, config=app_config, conn=conn)  # must not raise

    row = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["active"] == 1
    assert len(deps.unresolved_dependencies(mod_id, conn, dependency_type="optional")) == 1


def test_required_dependency_unresolved_when_target_disabled(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Needs Core")
    mod_manager.disable(mod_id, config=app_config, conn=conn)
    core_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    deps.add_dependency(mod_id, conn=conn, dependency_type="required", depends_on_mod_id=core_id)

    mod_manager.disable(core_id, config=app_config, conn=conn)  # now unresolved again

    with pytest.raises(deps.UnresolvedRequiredDependencyError):
        mod_manager.enable(mod_id, config=app_config, conn=conn)


def test_add_dependency_rejects_invalid_type(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Some Mod")

    with pytest.raises(deps.DependencyError):
        deps.add_dependency(mod_id, conn=conn, dependency_type="bogus", depends_on_mod_id=mod_id)


def test_add_dependency_requires_a_target(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Some Mod")

    with pytest.raises(deps.DependencyError):
        deps.add_dependency(mod_id, conn=conn, dependency_type="required")


# --- translation detection: description signal ------------------------------


def test_description_signal_finds_source_via_keyword_and_url(app_config, conn, tmp_path):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo")
    _set_links(conn, source_id, '{"curseforge_url": "https://www.curseforge.com/sims4/mods/better-woohoo"}')

    description = (
        "French translation for Better Woohoo. "
        "See https://www.curseforge.com/sims4/mods/better-woohoo for the original."
    )
    signal = deps.description_signal(description, conn)

    assert signal is not None
    assert signal.source_mod_id == source_id
    assert signal.method == "description"
    assert signal.strength == "strong"


def test_description_signal_none_without_keyword(app_config, conn, tmp_path):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo")
    _set_links(conn, source_id, '{"curseforge_url": "https://www.curseforge.com/sims4/mods/better-woohoo"}')

    description = "Just a link: https://www.curseforge.com/sims4/mods/better-woohoo"

    assert deps.description_signal(description, conn) is None


def test_description_signal_none_when_url_matches_nothing_installed(app_config, conn):
    description = "Traduction for https://www.curseforge.com/sims4/mods/some-other-mod"

    assert deps.description_signal(description, conn) is None


def test_description_signal_none_for_empty_description(app_config, conn):
    assert deps.description_signal("", conn) is None


# --- translation detection: name/slug heuristic -----------------------------


def test_name_heuristic_matches_bracket_marker(app_config, conn, tmp_path):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo")

    signal = deps.name_heuristic_signal("Better Woohoo [FR]", conn)

    assert signal is not None
    assert signal.source_mod_id == source_id
    assert signal.method == "name_heuristic"
    assert signal.strength == "weak"


def test_name_heuristic_matches_suffix_marker(app_config, conn, tmp_path):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo")

    signal = deps.name_heuristic_signal("Better Woohoo - French Translation", conn)

    assert signal is not None
    assert signal.source_mod_id == source_id


def test_name_heuristic_none_without_marker(app_config, conn, tmp_path):
    _install_mod(app_config, conn, tmp_path, "Better Woohoo")

    assert deps.name_heuristic_signal("Better Woohoo", conn) is None


def test_name_heuristic_none_when_no_close_match(app_config, conn, tmp_path):
    _install_mod(app_config, conn, tmp_path, "Better Woohoo")

    assert deps.name_heuristic_signal("Completely Unrelated Mod [FR]", conn) is None


# --- translation detection: STBL comparison ---------------------------------


def test_stbl_signal_confirms_shared_string_table(app_config, conn, dbpf_writer, tmp_path):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo", filename="source.package")
    candidate_id = _install_mod(
        app_config, conn, tmp_path, "Better Woohoo FR", filename="translation.package"
    )

    dbpf_writer(
        app_config.library_dir / source_id / "source.package",
        [(pp.RESOURCE_TYPE_STBL, 5, 999, b"en-text")],
    )
    dbpf_writer(
        app_config.library_dir / candidate_id / "translation.package",
        [(pp.RESOURCE_TYPE_STBL, 5, 999, b"fr-text")],
    )
    _rehash(conn, candidate_id, app_config.library_dir / candidate_id / "translation.package")
    _rehash(conn, source_id, app_config.library_dir / source_id / "source.package")

    signal = deps.stbl_signal(candidate_id, source_id, conn)

    assert signal is not None
    assert signal.method == "stbl_comparison"
    assert signal.strength == "strong"


def test_stbl_signal_none_when_no_shared_keys(app_config, conn, dbpf_writer, tmp_path):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo", filename="source.package")
    candidate_id = _install_mod(app_config, conn, tmp_path, "Unrelated", filename="other.package")

    dbpf_writer(
        app_config.library_dir / source_id / "source.package",
        [(pp.RESOURCE_TYPE_STBL, 5, 999, b"en-text")],
    )
    dbpf_writer(
        app_config.library_dir / candidate_id / "other.package",
        [(pp.RESOURCE_TYPE_STBL, 1, 111, b"unrelated")],
    )
    _rehash(conn, candidate_id, app_config.library_dir / candidate_id / "other.package")
    _rehash(conn, source_id, app_config.library_dir / source_id / "source.package")

    assert deps.stbl_signal(candidate_id, source_id, conn) is None


def test_stbl_signal_none_when_candidate_has_ts4script(app_config, conn, dbpf_writer, tmp_path):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo", filename="source.package")
    archive = tmp_path / "mixed.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("translation.package", b"placeholder")
        zf.writestr("script.ts4script", b"bytecode")
    candidate_id = mod_manager.install(archive, config=app_config, conn=conn, mod_name="Mixed FR")

    dbpf_writer(
        app_config.library_dir / source_id / "source.package",
        [(pp.RESOURCE_TYPE_STBL, 5, 999, b"en-text")],
    )
    _rehash(conn, source_id, app_config.library_dir / source_id / "source.package")

    assert deps.is_translation_candidate(candidate_id, conn) is False
    assert deps.stbl_signal(candidate_id, source_id, conn) is None


def test_is_translation_candidate_false_when_too_large(app_config, conn, tmp_path):
    candidate_id = _install_mod(
        app_config, conn, tmp_path, "Big Mod", filename="big.package", content=b"x" * 3_000_000
    )

    assert deps.is_translation_candidate(candidate_id, conn) is False


def _rehash(conn, mod_id, path):
    stat = path.stat()
    conn.execute(
        "UPDATE mod_files SET size = ?, hash = ? WHERE mod_id = ? AND relative_path = ?",
        (stat.st_size, mod_manager.hash_file(path), mod_id, path.name),
    )
    conn.commit()


# --- suggest / confirm / reject ---------------------------------------------


def test_suggest_translation_creates_suggested_link_never_confirmed(app_config, conn, tmp_path):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo")
    candidate_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo FR", filename="fr.package")

    dep_id = deps.suggest_translation(candidate_id, source_id, conn)

    links = deps.list_dependencies(candidate_id, conn)
    assert len(links) == 1
    assert links[0].id == dep_id
    assert links[0].confidence == "suggested"
    assert links[0].dependency_type == "translation"
    assert links[0].depends_on_mod_id == source_id


def test_confirm_dependency_flips_confidence_to_confirmed(app_config, conn, tmp_path):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo")
    candidate_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo FR", filename="fr.package")
    dep_id = deps.suggest_translation(candidate_id, source_id, conn)

    deps.confirm_dependency(dep_id, conn)

    links = deps.list_dependencies(candidate_id, conn)
    assert links[0].confidence == "confirmed"


def test_reject_dependency_removes_the_link(app_config, conn, tmp_path):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo")
    candidate_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo FR", filename="fr.package")
    dep_id = deps.suggest_translation(candidate_id, source_id, conn)

    deps.reject_dependency(dep_id, conn)

    assert deps.list_dependencies(candidate_id, conn) == []


def test_detect_translation_signals_combines_name_and_stbl(app_config, conn, dbpf_writer, tmp_path):
    source_id = _install_mod(app_config, conn, tmp_path, "Better Woohoo", filename="source.package")
    candidate_id = _install_mod(
        app_config, conn, tmp_path, "Better Woohoo [FR]", filename="translation.package"
    )
    dbpf_writer(
        app_config.library_dir / source_id / "source.package",
        [(pp.RESOURCE_TYPE_STBL, 5, 999, b"en-text")],
    )
    dbpf_writer(
        app_config.library_dir / candidate_id / "translation.package",
        [(pp.RESOURCE_TYPE_STBL, 5, 999, b"fr-text")],
    )
    _rehash(conn, candidate_id, app_config.library_dir / candidate_id / "translation.package")
    _rehash(conn, source_id, app_config.library_dir / source_id / "source.package")

    signals = deps.detect_translation_signals(candidate_id, conn)

    methods = {s.method for s in signals}
    assert "name_heuristic" in methods
    assert "stbl_comparison" in methods
    assert all(s.source_mod_id == source_id for s in signals)


def test_detect_translation_signals_empty_for_unrelated_mod(app_config, conn, tmp_path):
    _install_mod(app_config, conn, tmp_path, "Better Woohoo")
    candidate_id = _install_mod(app_config, conn, tmp_path, "Totally Different Mod", filename="x.package")

    assert deps.detect_translation_signals(candidate_id, conn) == []
