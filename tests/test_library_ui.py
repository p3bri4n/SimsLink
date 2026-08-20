"""Tests the pure control-construction/event-handling logic of ui/library.py
against real flet controls, using a stub Page (no live session/renderer is
available in this environment — see CLAUDE.md's testing note on this)."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from typing import Any

import flet as ft

import mod_manager
from i18n import translator
from ui import library as library_view

T = translator("en")


class FakePage:
    def __init__(self) -> None:
        self.update_calls = 0
        self.shown_dialogs: list[ft.AlertDialog] = []

    def update(self, *controls: Any) -> None:
        self.update_calls += 1

    def show_dialog(self, dialog: ft.AlertDialog) -> None:
        self.shown_dialogs.append(dialog)

    def pop_dialog(self) -> None:
        if self.shown_dialogs:
            self.shown_dialogs.pop()


@dataclass
class FakeEvent:
    control: Any = None


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


def _install_mod(app_config, conn, tmp_path, name="Cool Mod", filename="mymod.package") -> str:
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename, b"data")
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name)


def test_build_shows_empty_message_with_no_mods(app_config, conn):
    page = FakePage()

    view = library_view.build(config=app_config, conn=conn, page=page, t=T)

    texts = [c.value for c in _find(view.control, ft.Text)]
    assert T("library.empty") in texts


def test_build_shows_one_card_per_mod(app_config, conn, tmp_path):
    _install_mod(app_config, conn, tmp_path, name="Cool Mod")
    _install_mod(app_config, conn, tmp_path, name="Another Mod", filename="other.package")
    page = FakePage()

    view = library_view.build(config=app_config, conn=conn, page=page, t=T)

    assert len(_find(view.control, ft.Card)) == 2


def test_switch_off_disables_mod(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path)
    page = FakePage()
    view = library_view.build(config=app_config, conn=conn, page=page, t=T)

    switch = _find(view.control, ft.Switch)[0]
    assert switch.value is True
    switch.value = False
    switch.on_change(FakeEvent(control=switch))

    row = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["active"] == 0
    # refresh() rebuilds the grid, so the card should now render as inactive.
    switch_after = _find(view.control, ft.Switch)[0]
    assert switch_after.value is False


def test_info_button_shows_detail_dialog(app_config, conn, tmp_path):
    _install_mod(app_config, conn, tmp_path, name="Cool Mod")
    page = FakePage()
    view = library_view.build(config=app_config, conn=conn, page=page, t=T)

    info_button = _find(view.control, ft.IconButton)[0]
    info_button.on_click(FakeEvent())

    assert len(page.shown_dialogs) == 1
    dialog = page.shown_dialogs[0]
    assert dialog.title.value == "Cool Mod"


def test_delete_button_shows_confirmation_then_deletes(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, name="Cool Mod")
    page = FakePage()
    view = library_view.build(config=app_config, conn=conn, page=page, t=T)

    delete_button = _find(view.control, ft.IconButton)[1]
    delete_button.on_click(FakeEvent())

    assert len(page.shown_dialogs) == 1
    confirm_dialog = page.shown_dialogs[0]
    confirm_action = _find(confirm_dialog, ft.TextButton)[1]
    assert confirm_action.content == T("library.delete_confirm.confirm")

    confirm_action.on_click(FakeEvent())

    assert conn.execute("SELECT COUNT(*) FROM mods WHERE id = ?", (mod_id,)).fetchone()[0] == 0
    assert len(_find(view.control, ft.Card)) == 0
