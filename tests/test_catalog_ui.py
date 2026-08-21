"""Tests ui/catalog.py's control-construction/event-handling logic against
real flet controls with a stub Page (see tests/test_library_ui.py for why).
Direct Mode is exercised with a hand-rolled fake client (search_mods/
get_files/download) so nothing here touches the network or needs a real API
key, per CLAUDE.md's testing note on mode-dependent code."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import flet as ft

import curseforge
from i18n import translator
from ui import catalog as catalog_view

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
    def __init__(self, search_results=None, files_by_mod=None, *, fail_search=None):
        self._search_results = search_results or []
        self._files_by_mod = files_by_mod or {}
        self._fail_search = fail_search
        self.download_calls: list[tuple[int, int]] = []

    def search_mods(self, query: str, *, game_version=None):
        if self._fail_search is not None:
            raise self._fail_search
        return self._search_results

    def get_files(self, mod_id: int):
        return self._files_by_mod.get(mod_id, [])

    def download(self, mod_id: int, file_id: int, destination):
        self.download_calls.append((mod_id, file_id))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"data")
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


def _make_mod(**overrides) -> curseforge.CurseForgeMod:
    defaults = dict(
        mod_id=111,
        name="Better Woohoo",
        author="SomeAuthor",
        category="Gameplay",
        short_description="Makes it better.",
        thumbnail_url=None,
        curseforge_url="https://www.curseforge.com/sims4/mods/better-woohoo",
        third_party_distribution_allowed=True,
    )
    defaults.update(overrides)
    return curseforge.CurseForgeMod(**defaults)


def _do_search(control: Any, query: str = "woohoo") -> None:
    _find(control, ft.TextField)[0].value = query
    search_button = next(b for b in _find(control, ft.TextButton) if b.content == T("catalog.search_button"))
    search_button.on_click(FakeEvent())


# --- Assisted Mode (client=None) --------------------------------------------


def test_assisted_mode_shows_notice_and_no_search_field(app_config, conn):
    page = FakePage()

    control = catalog_view.build(
        config=app_config, conn=conn, page=page, t=T, client=None, on_installed=lambda: None
    )

    assert T("catalog.assisted_mode_notice") in _texts(control)
    assert not _find(control, ft.TextField)


# --- Direct Mode (client set) ------------------------------------------------


def test_direct_mode_search_renders_results(app_config, conn):
    page = FakePage()
    client = FakeCurseForgeClient(search_results=[_make_mod()])

    control = catalog_view.build(
        config=app_config, conn=conn, page=page, t=T, client=client, on_installed=lambda: None
    )
    _do_search(control)

    texts = _texts(control)
    assert "Better Woohoo" in texts
    assert "Makes it better." in texts


def test_direct_mode_search_shows_no_results_message(app_config, conn):
    page = FakePage()
    client = FakeCurseForgeClient(search_results=[])

    control = catalog_view.build(
        config=app_config, conn=conn, page=page, t=T, client=client, on_installed=lambda: None
    )
    _do_search(control)

    assert T("catalog.no_results") in _texts(control)


def test_direct_mode_search_error_is_shown_without_crashing(app_config, conn):
    page = FakePage()
    client = FakeCurseForgeClient(fail_search=curseforge.CurseForgeError("rate limited"))

    control = catalog_view.build(
        config=app_config, conn=conn, page=page, t=T, client=client, on_installed=lambda: None
    )
    _do_search(control)

    assert "rate limited" in _texts(control)


def test_direct_mode_install_button_downloads_and_installs_when_distribution_allowed(app_config, conn):
    page = FakePage()
    installed_calls = []
    mod = _make_mod(third_party_distribution_allowed=True)
    client = FakeCurseForgeClient(
        search_results=[mod],
        files_by_mod={
            111: [
                curseforge.CurseForgeFile(
                    file_id=222,
                    file_name="better-woohoo.package",
                    download_url="https://example.com/222",
                    game_version_min="1.90",
                    game_version_max="1.110",
                    release_type="release",
                )
            ]
        },
    )

    control = catalog_view.build(
        config=app_config,
        conn=conn,
        page=page,
        t=T,
        client=client,
        on_installed=lambda: installed_calls.append(1),
    )
    _do_search(control)
    install_button = next(b for b in _find(control, ft.TextButton) if b.content == T("catalog.install_button"))

    install_button.on_click(FakeEvent())

    assert client.download_calls == [(111, 222)]
    assert installed_calls == [1]
    row = conn.execute("SELECT * FROM mods WHERE curseforge_id = 111").fetchone()
    assert row is not None
    assert row["name"] == "Better Woohoo"
    assert row["installed_version"] == "222"
    assert row["links"] == json.dumps({"curseforge_url": mod.curseforge_url})


def test_direct_mode_shows_open_on_curseforge_when_distribution_not_allowed(app_config, conn):
    page = FakePage()
    mod = _make_mod(third_party_distribution_allowed=False)
    client = FakeCurseForgeClient(search_results=[mod])

    control = catalog_view.build(
        config=app_config, conn=conn, page=page, t=T, client=client, on_installed=lambda: None
    )
    _do_search(control)

    assert not [b for b in _find(control, ft.TextButton) if b.content == T("catalog.install_button")]
    open_button = next(
        b for b in _find(control, ft.TextButton) if b.content == T("catalog.open_on_curseforge_button")
    )

    open_button.on_click(FakeEvent())

    assert page.launched_urls == [mod.curseforge_url]
    assert conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 0


def test_direct_mode_install_error_shown_when_no_files_available(app_config, conn):
    page = FakePage()
    mod = _make_mod()
    client = FakeCurseForgeClient(search_results=[mod], files_by_mod={})

    control = catalog_view.build(
        config=app_config, conn=conn, page=page, t=T, client=client, on_installed=lambda: None
    )
    _do_search(control)
    install_button = next(b for b in _find(control, ft.TextButton) if b.content == T("catalog.install_button"))

    install_button.on_click(FakeEvent())

    assert any("No files available" in text for text in _texts(control))
    assert conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 0
