"""Tests ui/crash_mode.py's control-construction/event-handling logic against
real flet controls with a stub Page (see tests/test_library_ui.py for why)."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import flet as ft

import mod_manager
from i18n import translator
from ui import crash_mode as crash_mode_view

T = translator("en")
FIXTURES = Path(__file__).parent / "fixtures"


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


def _install_mod(app_config, conn, tmp_path, name, filename="mymod.package"):
    archive = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename, b"data")
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name)


def test_analyze_button_shows_no_exception_file_message(app_config, conn):
    page = FakePage()
    control = crash_mode_view.build(config=app_config, conn=conn, page=page, t=T)

    analyze_button = _find(control, ft.Button)[0]
    analyze_button.on_click(FakeEvent())

    texts = [c.value for c in _find(control, ft.Text)]
    assert T("crash.no_exception_file") in texts


def test_analyze_button_renders_direct_trace_suspect(app_config, conn, tmp_path):
    mod_id = _install_mod(app_config, conn, tmp_path, "bettermod", filename="bettermod.ts4script")
    (app_config.sims4_user_dir / "lastException.txt").write_text(
        (FIXTURES / "lastexception_mod_in_trace.txt").read_text()
    )
    page = FakePage()
    control = crash_mode_view.build(config=app_config, conn=conn, page=page, t=T)

    analyze_button = _find(control, ft.Button)[0]
    analyze_button.on_click(FakeEvent())

    texts = " ".join(c.value for c in _find(control, ft.Text) if c.value)
    assert "bettermod" in texts
    assert "direct_trace" in texts
    # A single occurrence must never surface a delete/disable action.
    assert not _find(control, ft.Switch)
    assert "delete" not in texts.lower()


def test_analyze_button_offers_bisection_when_no_suspect(app_config, conn, tmp_path):
    _install_mod(app_config, conn, tmp_path, "SomeMod")
    (app_config.sims4_user_dir / "lastException.txt").write_text(
        (FIXTURES / "lastexception_core_only.txt").read_text()
    )
    page = FakePage()
    control = crash_mode_view.build(config=app_config, conn=conn, page=page, t=T)

    analyze_button = _find(control, ft.Button)[0]
    analyze_button.on_click(FakeEvent())

    texts = [c.value for c in _find(control, ft.Text)]
    assert T("crash.no_suspects") in texts
    bisect_buttons = [b for b in _find(control, ft.TextButton) if b.content == T("crash.start_bisection")]
    assert len(bisect_buttons) == 1


def test_bisection_flow_converges_and_confirms(app_config, conn, tmp_path):
    mod_ids = [
        _install_mod(app_config, conn, tmp_path, f"Mod{i}", filename=f"mod{i}.package") for i in range(4)
    ]
    culprit = mod_ids[3]
    (app_config.sims4_user_dir / "lastException.txt").write_text(
        (FIXTURES / "lastexception_core_only.txt").read_text()
    )
    page = FakePage()
    control = crash_mode_view.build(config=app_config, conn=conn, page=page, t=T)

    _find(control, ft.Button)[0].on_click(FakeEvent())  # analyze -> no suspects
    start_button = next(
        b for b in _find(control, ft.TextButton) if b.content == T("crash.start_bisection")
    )
    start_button.on_click(FakeEvent())

    # Drive the bisection loop by always reporting "still crashes" unless the
    # disabled batch contains the culprit (mirrors the crash_analyzer test).
    for _ in range(3):
        buttons = {b.content: b for b in _find(control, ft.TextButton)}
        if T("crash.confirm_faulty") in buttons:
            break
        still_crashes_btn = buttons[T("crash.bisection_still_crashes")]
        fixed_btn = buttons[T("crash.bisection_fixed")]
        # Peek at DB state to know which batch got disabled this round.
        disabled_now = [
            row["id"]
            for row in conn.execute("SELECT id FROM mods WHERE active = 0").fetchall()
        ]
        if culprit in disabled_now:
            fixed_btn.on_click(FakeEvent())
        else:
            still_crashes_btn.on_click(FakeEvent())

    confirm_buttons = [b for b in _find(control, ft.TextButton) if b.content == T("crash.confirm_faulty")]
    assert len(confirm_buttons) == 1
    confirm_buttons[0].on_click(FakeEvent())

    row = conn.execute("SELECT confirmed_faulty_mod_id FROM crash_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["confirmed_faulty_mod_id"] == culprit
    # Still not deleted — confirmation only records, never destroys.
    assert conn.execute("SELECT COUNT(*) FROM mods WHERE id = ?", (culprit,)).fetchone()[0] == 1


def test_clear_cache_shows_dialog_and_clears_on_confirm(app_config, conn):
    (app_config.sims4_user_dir / "localthumbcache.package").write_bytes(b"x")
    page = FakePage()
    control = crash_mode_view.build(config=app_config, conn=conn, page=page, t=T)

    clear_button = _find(control, ft.TextButton)[0]
    clear_button.on_click(FakeEvent())

    assert len(page.shown_dialogs) == 1
    dialog = page.shown_dialogs[0]
    confirm_action = _find(dialog, ft.TextButton)[1]
    assert confirm_action.content == T("crash.clear_cache_confirm")

    confirm_action.on_click(FakeEvent())

    assert not (app_config.sims4_user_dir / "localthumbcache.package").exists()


def test_clear_cache_never_touches_protected_files(app_config, conn):
    (app_config.sims4_user_dir / "options.ini").write_bytes(b"x")
    page = FakePage()
    control = crash_mode_view.build(config=app_config, conn=conn, page=page, t=T)

    clear_button = _find(control, ft.TextButton)[0]
    clear_button.on_click(FakeEvent())
    dialog = page.shown_dialogs[0]
    confirm_action = _find(dialog, ft.TextButton)[1]
    confirm_action.on_click(FakeEvent())

    assert (app_config.sims4_user_dir / "options.ini").exists()
