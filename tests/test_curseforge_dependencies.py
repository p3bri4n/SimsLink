import zipfile

from backend import curseforge as cf
from backend import curseforge_dependencies as cfd
from backend import dependencies as deps
from backend import mod_manager

import pytest


class _FakeClient:
    """Stands in for curseforge.CurseForgeClient — only the two methods
    curseforge_dependencies.py actually calls (same convention as
    test_curseforge_match.py's own _FakeClient). `fail_for`/`auth_fail_for`
    make get_mod() raise for a specific curseforge_id, to test
    run_sync_step()'s per-mod error handling."""

    def __init__(
        self,
        mods_by_id: dict[int, cf.CurseForgeMod],
        files_by_key: dict[tuple[int, int], cf.CurseForgeFile],
        *,
        fail_for: set[int] = frozenset(),
        auth_fail_for: set[int] = frozenset(),
    ):
        self._mods_by_id = mods_by_id
        self._files_by_key = files_by_key
        self._fail_for = fail_for
        self._auth_fail_for = auth_fail_for

    def get_mod(self, mod_id):
        if mod_id in self._auth_fail_for:
            raise cf.CurseForgeAuthError("simulated auth failure")
        if mod_id in self._fail_for:
            raise cf.CurseForgeError("simulated transient failure")
        return self._mods_by_id[mod_id]

    def get_file(self, mod_id, file_id):
        return self._files_by_key[(mod_id, file_id)]


def _install_mod(app_config, conn, tmp_path, name, filename="mymod.package", content=b"data"):
    archive = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename, content)
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name)


def _link(conn, mod_id, curseforge_id):
    conn.execute("UPDATE mods SET curseforge_id = ? WHERE id = ?", (curseforge_id, mod_id))
    conn.commit()


def _make_mod(mod_id, main_file_id):
    return cf.CurseForgeMod(
        mod_id=mod_id,
        name=f"Mod {mod_id}",
        author=None,
        category=None,
        short_description="",
        thumbnail_url=None,
        curseforge_url=None,
        third_party_distribution_allowed=True,
        main_file_id=main_file_id,
    )


def _make_file(file_id, dependencies_, game_version_min=None, game_version_max=None):
    return cf.CurseForgeFile(
        file_id=file_id,
        file_name="mod.zip",
        download_url=None,
        game_version_min=game_version_min,
        game_version_max=game_version_max,
        release_type="release",
        dependencies=tuple(dependencies_),
    )


def test_required_dependency_resolves_to_installed_mod_creates_suggested_row(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    core_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    _link(conn, mod_id, 100)
    _link(conn, core_id, 200)
    client = _FakeClient(
        mods_by_id={100: _make_mod(100, main_file_id=999)},
        files_by_key={(100, 999): _make_file(999, [cf.CurseForgeFileDependency(mod_id=200, relation_type=3)])},
    )

    links = cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn)

    assert len(links) == 1
    assert links[0].dependency_type == "required"
    assert links[0].confidence == "suggested"
    assert links[0].depends_on_mod_id == core_id
    assert links[0].mandatory is True


def test_optional_dependency_resolves_similarly(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    core_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    _link(conn, mod_id, 100)
    _link(conn, core_id, 200)
    client = _FakeClient(
        mods_by_id={100: _make_mod(100, main_file_id=999)},
        files_by_key={(100, 999): _make_file(999, [cf.CurseForgeFileDependency(mod_id=200, relation_type=2)])},
    )

    links = cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn)

    assert links[0].dependency_type == "optional"
    assert links[0].confidence == "suggested"
    assert links[0].mandatory is False


def test_dependency_not_installed_locally_is_skipped(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    _link(conn, mod_id, 100)
    client = _FakeClient(
        mods_by_id={100: _make_mod(100, main_file_id=999)},
        files_by_key={(100, 999): _make_file(999, [cf.CurseForgeFileDependency(mod_id=200, relation_type=3)])},
    )

    links = cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn)

    assert links == []


def test_unlinked_mod_raises(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")  # curseforge_id stays NULL
    client = _FakeClient(mods_by_id={}, files_by_key={})

    with pytest.raises(cfd.CurseForgeDependenciesError):
        cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn)


def test_rerunning_detection_does_not_duplicate_rows(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    core_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    _link(conn, mod_id, 100)
    _link(conn, core_id, 200)
    client = _FakeClient(
        mods_by_id={100: _make_mod(100, main_file_id=999)},
        files_by_key={(100, 999): _make_file(999, [cf.CurseForgeFileDependency(mod_id=200, relation_type=3)])},
    )

    cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn)
    links = cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn)

    assert len(links) == 1


def test_confirmed_dependency_is_not_duplicated_by_rerun(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    core_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    _link(conn, mod_id, 100)
    _link(conn, core_id, 200)
    dependency_id = deps.add_dependency(mod_id, conn=conn, dependency_type="required", depends_on_mod_id=core_id)
    deps.confirm_dependency(dependency_id, conn)
    client = _FakeClient(
        mods_by_id={100: _make_mod(100, main_file_id=999)},
        files_by_key={(100, 999): _make_file(999, [cf.CurseForgeFileDependency(mod_id=200, relation_type=3)])},
    )

    links = cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn)

    assert len(links) == 1
    assert links[0].confidence == "confirmed"  # untouched, not overwritten/duplicated by the suggestion


def test_unsupported_relation_types_are_skipped(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    core_id = _install_mod(app_config, conn, tmp_path, "Core Lib", filename="core.package")
    _link(conn, mod_id, 100)
    _link(conn, core_id, 200)
    client = _FakeClient(
        mods_by_id={100: _make_mod(100, main_file_id=999)},
        files_by_key={
            (100, 999): _make_file(
                999,
                [
                    cf.CurseForgeFileDependency(mod_id=200, relation_type=1),  # EmbeddedLibrary
                    cf.CurseForgeFileDependency(mod_id=200, relation_type=5),  # Incompatible
                ],
            )
        },
    )

    links = cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn)

    assert links == []


def test_self_reference_is_skipped(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    _link(conn, mod_id, 100)
    client = _FakeClient(
        mods_by_id={100: _make_mod(100, main_file_id=999)},
        files_by_key={(100, 999): _make_file(999, [cf.CurseForgeFileDependency(mod_id=100, relation_type=3)])},
    )

    links = cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn)

    assert links == []


def test_no_main_file_id_returns_current_list_without_error(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    _link(conn, mod_id, 100)
    client = _FakeClient(mods_by_id={100: _make_mod(100, main_file_id=None)}, files_by_key={})

    links = cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn)

    assert links == []


# --- compat_status / game_version_min/max ------------------------------------------


def test_fetch_and_suggest_dependencies_updates_compat_status(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    _link(conn, mod_id, 100)
    client = _FakeClient(
        mods_by_id={100: _make_mod(100, main_file_id=999)},
        files_by_key={(100, 999): _make_file(999, [], game_version_min="1.90", game_version_max="1.110")},
    )

    cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn, game_version="1.126")

    row = conn.execute(
        "SELECT compat_status, game_version_min, game_version_max FROM mods WHERE id = ?", (mod_id,)
    ).fetchone()
    assert row["compat_status"] == "incompatible"
    assert row["game_version_min"] == "1.90"
    assert row["game_version_max"] == "1.110"


def test_fetch_and_suggest_dependencies_skips_compat_update_without_game_version(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    _link(conn, mod_id, 100)
    client = _FakeClient(
        mods_by_id={100: _make_mod(100, main_file_id=999)},
        files_by_key={(100, 999): _make_file(999, [], game_version_min="1.90", game_version_max="1.110")},
    )

    cfd.fetch_and_suggest_dependencies(mod_id, client=client, conn=conn)  # no game_version passed

    row = conn.execute("SELECT compat_status FROM mods WHERE id = ?", (mod_id,)).fetchone()
    # compat_status() itself returns "unknown" when current_version is None —
    # this isn't skipped, it's just the correct classification for "we don't
    # know the game version to check against."
    assert row["compat_status"] == "unknown"


# --- bulk sync (SyncSession) ---------------------------------------------------------


def test_start_sync_session_counts_all_linked_mods(app_config, conn, tmp_path):
    linked_id = _install_mod(app_config, conn, tmp_path, "Linked")
    _link(conn, linked_id, 100)
    _install_mod(app_config, conn, tmp_path, "Unlinked")  # curseforge_id stays NULL

    session = cfd.start_sync_session(conn)

    assert session.total == 1
    assert session.remaining == [linked_id]
    assert not session.done


def test_run_sync_step_processes_chunk_and_updates_compat(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    _link(conn, mod_id, 100)
    client = _FakeClient(
        mods_by_id={100: _make_mod(100, main_file_id=999)},
        files_by_key={(100, 999): _make_file(999, [], game_version_min="1.90", game_version_max="1.130")},
    )
    session = cfd.start_sync_session(conn)

    cfd.run_sync_step(session, conn, client, "1.126")

    assert session.checked == 1
    assert session.synced == 1
    assert session.errors == 0
    assert session.done
    row = conn.execute("SELECT compat_status FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["compat_status"] == "compatible"


def test_run_sync_step_continues_past_a_single_mod_transient_error(app_config, conn, tmp_path):
    ok_id = _install_mod(app_config, conn, tmp_path, "OK Mod", filename="ok.package")
    bad_id = _install_mod(app_config, conn, tmp_path, "Bad Mod", filename="bad.package")
    _link(conn, ok_id, 100)
    _link(conn, bad_id, 200)
    client = _FakeClient(
        mods_by_id={100: _make_mod(100, main_file_id=999)},
        files_by_key={(100, 999): _make_file(999, [])},
        fail_for={200},
    )
    session = cfd.start_sync_session(conn)

    cfd.run_sync_step(session, conn, client, None, chunk_size=10)

    assert session.checked == 2
    assert session.synced == 1
    assert session.errors == 1
    assert session.done


def test_run_sync_step_reraises_auth_error_and_stops(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "Mod A")
    _link(conn, mod_id, 100)
    client = _FakeClient(mods_by_id={}, files_by_key={}, auth_fail_for={100})
    session = cfd.start_sync_session(conn)

    with pytest.raises(cf.CurseForgeAuthError):
        cfd.run_sync_step(session, conn, client, None)


def test_run_sync_step_respects_chunk_size(app_config, conn, tmp_path):
    ids = []
    for i in range(3):
        mod_id = _install_mod(app_config, conn, tmp_path, f"Mod {i}", filename=f"mod{i}.package")
        _link(conn, mod_id, 100 + i)
        ids.append(mod_id)
    files_by_key = {(100 + i, 999): _make_file(999, []) for i in range(3)}
    mods_by_id = {100 + i: _make_mod(100 + i, main_file_id=999) for i in range(3)}
    client = _FakeClient(mods_by_id=mods_by_id, files_by_key=files_by_key)
    session = cfd.start_sync_session(conn)

    cfd.run_sync_step(session, conn, client, None, chunk_size=2)

    assert session.checked == 2
    assert session.synced == 2
    assert not session.done
    assert len(session.remaining) == 1
