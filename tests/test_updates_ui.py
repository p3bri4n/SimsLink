"""Tests ui/updates.py's control-construction/event-handling logic against
real flet controls with a stub Page (see tests/test_library_ui.py for why).
Direct Mode is exercised with a hand-rolled fake client (get_files/get_mod/
download) so nothing here touches the network or needs a real API key, per
CLAUDE.md's testing note on mode-dependent code."""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import flet as ft

import curseforge
import mod_manager
from i18n import translator
from ui import updates as updates_view

T = translator("en")


@dataclass
class FakeEvent:
    control: Any = None


class FakePage:
    def __init__(self) -> None:
        self.update_calls = 0
        self.launched_urls: list[str] = []

    def update(self, *controls: Any) -> None:
        self.update_calls += 1

    def launch_url(self, url: str) -> None:
        self.launched_urls.append(url)


class FakeCurseForgeClient:
    def __init__(self, files_by_mod=None, mod_by_id=None, *, fail_mods=None):
        self._files_by_mod = files_by_mod or {}
        self._mod_by_id = mod_by_id or {}
        self._fail_mods = fail_mods or set()
        self.download_calls: list[tuple[int, int]] = []

    def get_files(self, mod_id: int):
        if mod_id in self._fail_mods:
            raise curseforge.CurseForgeError(f"boom {mod_id}")
        return self._files_by_mod.get(mod_id, [])

    def get_mod(self, mod_id: int):
        return self._mod_by_id[mod_id]

    def download(self, mod_id: int, file_id: int, destination: Path) -> Path:
        self.download_calls.append((mod_id, file_id))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"new-file-data")
        return destination


def _iter_controls(control: Any):
    yield control
    for attr in ("content", "title"):
        child = getattr(control, attr, None)
        if child is not None:
            yield from _iter_controls(child)
    for attr in ("controls", "actions"):
        for child in getattr(control, attr, None) or []:
            yield from _iter_controls(child)


def _find(control: Any, cls: type) -> list:
    return [c for c in _iter_controls(control) if isinstance(c, cls)]


def _texts(control: Any) -> list[str]:
    return [c.value for c in _find(control, ft.Text) if c.value]


def _install_mod(app_config, conn, tmp_path, name, *, metadata=mod_manager.ModMetadata(), filename=None):
    archive = tmp_path / f"{name}-src.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename or "mymod.package", b"data")
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name, metadata=metadata)


# --- Assisted Mode (client=None) --------------------------------------------


def test_assisted_mode_shows_notice_when_no_linked_mods(app_config, conn, tmp_path):
    _install_mod(app_config, conn, tmp_path, "Plain Mod")
    page = FakePage()

    control = updates_view.build(
        config=app_config, conn=conn, page=page, t=T, client=None, on_updated=lambda: None
    )

    assert T("updates.empty_assisted") in _texts(control)
    assert not _find(control, ft.TextButton)


def test_assisted_mode_lists_check_on_curseforge_for_linked_mods(app_config, conn, tmp_path):
    metadata = mod_manager.ModMetadata(links=json.dumps({"curseforge_url": "https://www.curseforge.com/x"}))
    _install_mod(app_config, conn, tmp_path, "Linked Mod", metadata=metadata)
    _install_mod(app_config, conn, tmp_path, "Unlinked Mod", filename="other.package")
    page = FakePage()

    control = updates_view.build(
        config=app_config, conn=conn, page=page, t=T, client=None, on_updated=lambda: None
    )

    assert "Linked Mod" in _texts(control)
    assert "Unlinked Mod" not in _texts(control)
    buttons = [b for b in _find(control, ft.TextButton) if b.content == T("updates.check_on_curseforge_button")]
    assert len(buttons) == 1

    buttons[0].on_click(FakeEvent())

    assert page.launched_urls == ["https://www.curseforge.com/x"]


# --- Direct Mode (client set) ------------------------------------------------


def test_direct_mode_empty_when_no_curseforge_linked_mods(app_config, conn, tmp_path):
    _install_mod(app_config, conn, tmp_path, "Plain Mod")
    page = FakePage()
    client = FakeCurseForgeClient()

    control = updates_view.build(
        config=app_config, conn=conn, page=page, t=T, client=client, on_updated=lambda: None
    )
    check_button = _find(control, ft.TextButton)[0]
    check_button.on_click(FakeEvent())

    assert T("updates.empty_direct") in _texts(control)


def test_direct_mode_reports_up_to_date_when_installed_version_matches_latest(app_config, conn, tmp_path):
    metadata = mod_manager.ModMetadata(curseforge_id=111, installed_version="222")
    _install_mod(app_config, conn, tmp_path, "Current Mod", metadata=metadata)
    page = FakePage()
    client = FakeCurseForgeClient(
        files_by_mod={
            111: [
                curseforge.CurseForgeFile(
                    file_id=222,
                    file_name="current.zip",
                    download_url="https://example.com/222",
                    game_version_min="1.90",
                    game_version_max="1.110",
                    release_type="release",
                )
            ]
        }
    )

    control = updates_view.build(
        config=app_config, conn=conn, page=page, t=T, client=client, on_updated=lambda: None
    )
    _find(control, ft.TextButton)[0].on_click(FakeEvent())

    assert T("updates.no_updates") in _texts(control)
    assert not [b for b in _find(control, ft.TextButton) if b.content == T("updates.update_button")]


def test_direct_mode_detects_and_applies_update(app_config, conn, tmp_path):
    metadata = mod_manager.ModMetadata(curseforge_id=111, installed_version="222")
    mod_id = _install_mod(app_config, conn, tmp_path, "Outdated Mod", metadata=metadata)
    page = FakePage()
    updated_calls = []
    client = FakeCurseForgeClient(
        files_by_mod={
            111: [
                curseforge.CurseForgeFile(
                    file_id=333,
                    file_name="outdated-v2.package",
                    download_url="https://example.com/333",
                    game_version_min="1.90",
                    game_version_max="1.120",
                    release_type="release",
                )
            ]
        },
        mod_by_id={
            111: curseforge.CurseForgeMod(
                mod_id=111,
                name="Outdated Mod",
                author="Someone",
                category="Gameplay",
                short_description="desc",
                thumbnail_url=None,
                curseforge_url="https://www.curseforge.com/sims4/mods/outdated-mod",
                third_party_distribution_allowed=True,
            )
        },
    )

    control = updates_view.build(
        config=app_config, conn=conn, page=page, t=T, client=client, on_updated=lambda: updated_calls.append(1)
    )
    _find(control, ft.TextButton)[0].on_click(FakeEvent())  # check
    update_button = next(b for b in _find(control, ft.TextButton) if b.content == T("updates.update_button"))

    update_button.on_click(FakeEvent())

    assert client.download_calls == [(111, 333)]
    assert updated_calls == [1]
    row = conn.execute("SELECT * FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["installed_version"] == "333"
    assert row["links"] == json.dumps({"curseforge_url": "https://www.curseforge.com/sims4/mods/outdated-mod"})


def test_direct_mode_check_reports_per_mod_error_without_aborting(app_config, conn, tmp_path):
    metadata = mod_manager.ModMetadata(curseforge_id=111, installed_version="222")
    _install_mod(app_config, conn, tmp_path, "Broken Link Mod", metadata=metadata)
    page = FakePage()
    client = FakeCurseForgeClient(fail_mods={111})

    control = updates_view.build(
        config=app_config, conn=conn, page=page, t=T, client=client, on_updated=lambda: None
    )
    _find(control, ft.TextButton)[0].on_click(FakeEvent())

    assert any("Broken Link Mod" in text for text in _texts(control))
