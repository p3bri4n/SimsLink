"""CurseForge catalog view (Direct Mode). In Assisted Mode this just
explains that browsing needs a valid API key — installs still go through
the download-watcher flow (brief section 4bis)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Callable

import flet as ft

import curseforge
import mod_manager
from config import Config

_COMPAT_ICONS = {"compatible": "🟢", "incompatible": "🔴", "unknown": "⚪"}


def build(
    *,
    config: Config,
    conn: sqlite3.Connection,
    page: ft.Page,
    t: Callable[..., str],
    client: curseforge.CurseForgeClient | None,
    on_installed: Callable[[], None],
) -> ft.Control:
    if client is None:
        return ft.Container(padding=20, content=ft.Text(t("catalog.assisted_mode_notice")))

    results = ft.Column(controls=[])
    search_field = ft.TextField(hint_text=t("catalog.search_placeholder"), expand=True)

    def do_search(e: ft.ControlEvent) -> None:
        try:
            mods = client.search_mods(search_field.value or "", game_version=config.game_version)
        except curseforge.CurseForgeError as exc:
            results.controls = [ft.Text(str(exc))]
            page.update()
            return
        results.controls = [_mod_row(mod) for mod in mods] if mods else [ft.Text(t("catalog.no_results"))]
        page.update()

    def _open_external(mod: curseforge.CurseForgeMod) -> None:
        if mod.curseforge_url:
            page.launch_url(mod.curseforge_url)

    def do_install(mod: curseforge.CurseForgeMod) -> None:
        try:
            files = client.get_files(mod.mod_id)
            if not files:
                raise curseforge.CurseForgeError(f"No files available for {mod.name}")
            latest = files[0]
            with tempfile.TemporaryDirectory(prefix="simslink-cf-") as tmp:
                downloaded = client.download(mod.mod_id, latest.file_id, Path(tmp) / latest.file_name)
                metadata = mod_manager.ModMetadata(
                    curseforge_id=mod.mod_id,
                    author=mod.author,
                    category=mod.category,
                    installed_version=str(latest.file_id),
                    compat_status=curseforge.compat_status(
                        latest.game_version_min, latest.game_version_max, config.game_version
                    ),
                    short_description=mod.short_description,
                    thumbnail_url=mod.thumbnail_url,
                    links=json.dumps({"curseforge_url": mod.curseforge_url}) if mod.curseforge_url else None,
                    game_version_min=latest.game_version_min,
                    game_version_max=latest.game_version_max,
                    third_party_distribution_allowed=mod.third_party_distribution_allowed,
                )
                mod_manager.install(downloaded, config=config, conn=conn, mod_name=mod.name, metadata=metadata)
            on_installed()
        except (curseforge.CurseForgeError, mod_manager.ModManagerError) as exc:
            results.controls.insert(0, ft.Text(t("catalog.install_error", error=str(exc))))
            page.update()

    def _mod_row(mod: curseforge.CurseForgeMod) -> ft.Control:
        if mod.third_party_distribution_allowed:
            action: ft.Control = ft.TextButton(
                content=t("catalog.install_button"), on_click=lambda e, m=mod: do_install(m)
            )
        else:
            action = ft.TextButton(
                content=t("catalog.open_on_curseforge_button"),
                on_click=lambda e, m=mod: _open_external(m),
            )
        return ft.Row(
            controls=[
                ft.Text(_COMPAT_ICONS["unknown"], tooltip=t("catalog.compat_unknown")),
                ft.Column(
                    controls=[
                        ft.Text(mod.name, weight=ft.FontWeight.BOLD),
                        ft.Text(mod.short_description, size=11),
                    ],
                    expand=True,
                ),
                action,
            ]
        )

    return ft.Container(
        padding=20,
        content=ft.Column(
            controls=[
                ft.Row(controls=[search_field, ft.TextButton(content=t("catalog.search_button"), on_click=do_search)]),
                results,
            ]
        ),
    )
