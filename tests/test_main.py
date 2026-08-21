"""Tests main.py's own orchestration logic (mode banner, nav, dialog wiring,
and — critically — that a download detected on the watcher's background
thread gets its DB matching done on the page's own thread via run_task, not
on the watcher thread) against a stub Page, since no real client/renderer is
available in this environment (see CLAUDE.md's testing note on this)."""

from __future__ import annotations

import asyncio
import queue
import zipfile
from pathlib import Path
from typing import Any

import flet as ft
import pytest

import config
import curseforge
import main


class FakeWindow:
    width: float | None = None
    height: float | None = None


class FakePage:
    def __init__(self) -> None:
        self.title: str | None = None
        self.window = FakeWindow()
        self.controls: list[Any] = []
        self.shown_dialogs: list[ft.AlertDialog] = []
        self.update_calls = 0
        self.on_disconnect = None
        self.pending_tasks: "queue.Queue[tuple]" = queue.Queue()

    def add(self, *controls: Any) -> None:
        self.controls.extend(controls)

    def update(self, *controls: Any) -> None:
        self.update_calls += 1

    def show_dialog(self, dialog: ft.AlertDialog) -> None:
        self.shown_dialogs.append(dialog)

    def pop_dialog(self) -> None:
        if self.shown_dialogs:
            self.shown_dialogs.pop()

    def run_task(self, handler, *args) -> None:
        # Real Flet marshals this onto the page's own event loop/thread.
        # We record it instead of running it inline so tests can execute it
        # from the *test's* thread — proving conn access happens off the
        # watcher's background thread, exactly like the real Page would do.
        self.pending_tasks.put((handler, args))

    def run_pending_task(self, timeout: float = 5) -> None:
        handler, args = self.pending_tasks.get(timeout=timeout)
        asyncio.run(handler(*args))


def _set_env(monkeypatch, tmp_path) -> dict[str, Path]:
    dirs = {
        "SIMS4_GAME_DIR": tmp_path / "game",
        "SIMS4_MODS_DIR": tmp_path / "user" / "Mods",
        "SIMS4_USER_DIR": tmp_path / "user",
        "LIBRARY_DIR": tmp_path / "library",
        "DOWNLOAD_WATCH_DIR": tmp_path / "downloads",
    }
    for value in dirs.values():
        value.mkdir(parents=True, exist_ok=True)
    for key, value in dirs.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv("CURSEFORGE_API_KEY", raising=False)
    monkeypatch.delenv("GAME_VERSION", raising=False)
    return dirs


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in (
        "SIMS4_GAME_DIR",
        "SIMS4_MODS_DIR",
        "SIMS4_USER_DIR",
        "LIBRARY_DIR",
        "DOWNLOAD_WATCH_DIR",
        "CURSEFORGE_API_KEY",
        "GAME_VERSION",
    ):
        monkeypatch.delenv(var, raising=False)
    # config.DEFAULT_ENV_PATH is resolved next to config.py (the real project
    # root) precisely so it doesn't depend on cwd — which means these tests
    # must not depend on whatever .env happens to exist there either.
    monkeypatch.setattr(config, "DEFAULT_ENV_PATH", tmp_path / ".env-not-created")


def test_main_shows_config_error_when_env_missing():
    page = FakePage()

    main.main(page)

    assert any("Configuration error" in str(getattr(c, "value", "")) for c in page.controls)


def test_main_shows_assisted_mode_banner_with_no_api_key(monkeypatch, tmp_path):
    _set_env(monkeypatch, tmp_path)
    page = FakePage()

    main.main(page)
    try:
        banner_texts = [c.value for c in _iter(page.controls[0]) if isinstance(c, ft.Text)]
        assert any("Assisted Mode" in t for t in banner_texts)
    finally:
        if page.on_disconnect is not None:
            page.on_disconnect(None)


class _FakeCurseForgeClient:
    """Stands in for curseforge.CurseForgeClient so these tests never hit the
    network — only verify_key()'s return value matters for main.py's mode
    detection (see CLAUDE.md: mode-dependent code must be testable without a
    real API key)."""

    def __init__(self, api_key, *, valid: bool) -> None:
        self.api_key = api_key
        self._valid = valid

    def verify_key(self) -> bool:
        return self._valid


def test_main_shows_direct_mode_banner_with_valid_api_key(monkeypatch, tmp_path):
    _set_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CURSEFORGE_API_KEY", "a-valid-key")
    monkeypatch.setattr(
        curseforge, "CurseForgeClient", lambda api_key: _FakeCurseForgeClient(api_key, valid=True)
    )
    page = FakePage()

    main.main(page)
    try:
        banner_texts = [c.value for c in _iter(page.controls[0]) if isinstance(c, ft.Text)]
        assert any("Direct Mode" in t for t in banner_texts)
    finally:
        if page.on_disconnect is not None:
            page.on_disconnect(None)


def test_main_falls_back_to_assisted_mode_when_api_key_invalid(monkeypatch, tmp_path):
    _set_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CURSEFORGE_API_KEY", "an-expired-key")
    monkeypatch.setattr(
        curseforge, "CurseForgeClient", lambda api_key: _FakeCurseForgeClient(api_key, valid=False)
    )
    page = FakePage()

    main.main(page)
    try:
        banner_texts = [c.value for c in _iter(page.controls[0]) if isinstance(c, ft.Text)]
        assert any("Assisted Mode" in t for t in banner_texts)
    finally:
        if page.on_disconnect is not None:
            page.on_disconnect(None)


def test_main_download_detection_matches_on_page_thread_not_watcher_thread(monkeypatch, tmp_path):
    dirs = _set_env(monkeypatch, tmp_path)
    page = FakePage()

    main.main(page)
    try:
        archive = dirs["DOWNLOAD_WATCH_DIR"] / "New.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("new.package", b"data")

        # Runs the dispatched coroutine here, on the test's (page-owning)
        # thread — matching what Page.run_task would do in a real app.
        page.run_pending_task(timeout=5)

        assert len(page.shown_dialogs) == 1
    finally:
        page.on_disconnect(None)


def _iter(control: Any):
    yield control
    for attr in ("content", "title"):
        child = getattr(control, attr, None)
        if child is not None:
            yield from _iter(child)
    for attr in ("controls", "actions"):
        for child in getattr(control, attr, None) or []:
            yield from _iter(child)
