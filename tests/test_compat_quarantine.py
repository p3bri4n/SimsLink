import zipfile

from backend import compat_quarantine as cq
from backend import dependencies as deps
from backend import mod_manager


def _install_mod(app_config, conn, tmp_path, name, filename="mymod.package", content=b"data"):
    archive = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename, content)
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name)


def _set_compat(conn, mod_id, status):
    conn.execute("UPDATE mods SET compat_status = ? WHERE id = ?", (status, mod_id))
    conn.commit()


def _set_curseforge_id(conn, mod_id, curseforge_id):
    conn.execute("UPDATE mods SET curseforge_id = ? WHERE id = ?", (curseforge_id, mod_id))
    conn.commit()


def test_preview_quarantine_finds_active_incompatible_mods(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "stale_mod")
    _set_compat(conn, mod_id, "incompatible")

    candidates = cq.preview_quarantine(conn)

    assert [c.mod_id for c in candidates] == [mod_id]
    assert candidates[0].reason == "incompatible"


def test_preview_quarantine_ignores_inactive_incompatible_mods(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "stale_mod")
    _set_compat(conn, mod_id, "incompatible")
    mod_manager.disable(mod_id, config=app_config, conn=conn)

    assert cq.preview_quarantine(conn) == []


def test_preview_quarantine_cascades_to_active_required_dependent(app_config, conn, tmp_path):
    core_id = _install_mod(app_config, conn, tmp_path, "core_lib")
    _set_compat(conn, core_id, "incompatible")
    _set_curseforge_id(conn, core_id, 111)

    dependent_id = _install_mod(app_config, conn, tmp_path, "dependent_mod")
    deps.add_dependency(
        dependent_id,
        conn=conn,
        dependency_type="required",
        depends_on_curseforge_id=111,
        confidence="confirmed",
    )

    candidates = {c.mod_id: c for c in cq.preview_quarantine(conn)}

    assert set(candidates) == {core_id, dependent_id}
    assert candidates[core_id].reason == "incompatible"
    assert candidates[dependent_id].reason == core_id


def test_preview_quarantine_does_not_cascade_to_inactive_dependent(app_config, conn, tmp_path):
    core_id = _install_mod(app_config, conn, tmp_path, "core_lib")
    _set_compat(conn, core_id, "incompatible")
    _set_curseforge_id(conn, core_id, 111)

    dependent_id = _install_mod(app_config, conn, tmp_path, "dependent_mod")
    deps.add_dependency(
        dependent_id, conn=conn, dependency_type="required", depends_on_curseforge_id=111
    )
    mod_manager.disable(dependent_id, config=app_config, conn=conn)

    candidates = {c.mod_id for c in cq.preview_quarantine(conn)}
    assert candidates == {core_id}


def test_preview_quarantine_ignores_optional_dependents(app_config, conn, tmp_path):
    core_id = _install_mod(app_config, conn, tmp_path, "core_lib")
    _set_compat(conn, core_id, "incompatible")
    _set_curseforge_id(conn, core_id, 111)

    dependent_id = _install_mod(app_config, conn, tmp_path, "optional_dependent")
    deps.add_dependency(
        dependent_id, conn=conn, dependency_type="optional", depends_on_curseforge_id=111
    )

    candidates = {c.mod_id for c in cq.preview_quarantine(conn)}
    assert candidates == {core_id}


def test_quarantine_mods_disables_and_records(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "stale_mod")
    _set_compat(conn, mod_id, "incompatible")
    candidates = cq.preview_quarantine(conn)

    quarantined = cq.quarantine_mods(candidates, config=app_config, conn=conn)

    assert quarantined == [mod_id]
    row = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert not row["active"]
    tracked = cq.list_quarantined(conn)
    assert [t["mod_id"] for t in tracked] == [mod_id]
    assert tracked[0]["reason"] == "incompatible"


def test_quarantine_mods_skips_already_inactive_candidate(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "stale_mod")
    _set_compat(conn, mod_id, "incompatible")
    mod_manager.disable(mod_id, config=app_config, conn=conn)
    candidate = cq.QuarantineCandidate(mod_id=mod_id, name="stale_mod", reason="incompatible")

    quarantined = cq.quarantine_mods([candidate], config=app_config, conn=conn)

    assert quarantined == []
    assert cq.list_quarantined(conn) == []


def test_release_ready_mods_reenables_once_compat_status_clears(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "stale_mod")
    _set_compat(conn, mod_id, "incompatible")
    cq.quarantine_mods(cq.preview_quarantine(conn), config=app_config, conn=conn)

    # Still incompatible: release_ready_mods() must leave it alone.
    result = cq.release_ready_mods(config=app_config, conn=conn)
    assert result == {"released": [], "still_incompatible": [mod_id], "failed": []}
    assert not conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()["active"]

    # A CurseForge resync clears compat_status — now it should come back.
    _set_compat(conn, mod_id, "compatible")
    result = cq.release_ready_mods(config=app_config, conn=conn)
    assert result == {"released": [mod_id], "still_incompatible": [], "failed": []}
    assert conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()["active"]
    assert cq.list_quarantined(conn) == []


def test_regression_release_ready_mods_does_not_drop_mod_blocked_on_confirmed_dependency(
    app_config, conn, tmp_path
):
    """A quarantined mod whose compat_status recovers but still has an
    unresolved *confirmed* required dependency must stay quarantined (and be
    reported under 'failed') rather than being silently dropped from
    tracking without actually being re-enabled — dropping it here would lose
    the only record telling the user it still needs attention."""
    core_id = _install_mod(app_config, conn, tmp_path, "core_lib")
    dependent_id = _install_mod(app_config, conn, tmp_path, "dependent_mod")
    deps.add_dependency(
        dependent_id, conn=conn, dependency_type="required", depends_on_mod_id=core_id
    )
    _set_compat(conn, dependent_id, "incompatible")
    mod_manager.disable(core_id, config=app_config, conn=conn)  # required dep now unresolved
    cq.quarantine_mods(cq.preview_quarantine(conn), config=app_config, conn=conn)

    _set_compat(conn, dependent_id, "compatible")
    result = cq.release_ready_mods(config=app_config, conn=conn)

    assert result["released"] == []
    assert result["failed"] and result["failed"][0]["mod_id"] == dependent_id
    tracked = {t["mod_id"] for t in cq.list_quarantined(conn)}
    assert dependent_id in tracked


def test_forget_quarantined_stops_tracking_without_touching_active_state(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "stale_mod")
    _set_compat(conn, mod_id, "incompatible")
    cq.quarantine_mods(cq.preview_quarantine(conn), config=app_config, conn=conn)

    cq.forget_quarantined(mod_id, conn)

    assert cq.list_quarantined(conn) == []
    assert not conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()["active"]
