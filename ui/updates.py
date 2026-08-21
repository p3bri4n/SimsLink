"""Updates view: Direct Mode auto-update list, Assisted Mode manual checklist.

Direct Mode compares each CurseForge-linked mod's stored installed_version
against the latest file CurseForge currently serves. That check only runs
when the user clicks "Check for updates" — never eagerly inside build(),
since build() runs on every rebuild() (including app startup and language
switches) and the check is a network call.

Assisted Mode has no API access, so there is no way to compare versions
automatically. Per CLAUDE.md, that must be surfaced, never silently
degraded: this renders a manual checklist instead — each installed mod with
a known CurseForge URL (stored on install, see mod_manager.ModMetadata.links)
gets a "Check on CurseForge" link. A newer file dropped in the download
folder is still picked up automatically by download_watcher.py, exactly like
a fresh install.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Callable

import flet as ft

import curseforge
import download_watcher
import mod_manager
from config import Config


def build(
    *,
    config: Config,
    conn: sqlite3.Connection,
    page: ft.Page,
    t: Callable[..., str],
    client: curseforge.CurseForgeClient | None,
    on_updated: Callable[[], None],
) -> ft.Control:
    if client is None:
        return _build_assisted(conn=conn, page=page, t=t)
    return _build_direct(config=config, conn=conn, page=page, t=t, client=client, on_updated=on_updated)


def _installed_mods(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM mods ORDER BY name COLLATE NOCASE").fetchall()


def _curseforge_url(row: sqlite3.Row) -> str | None:
    if not row["links"]:
        return None
    return (json.loads(row["links"]) or {}).get("curseforge_url")


def _build_assisted(*, conn: sqlite3.Connection, page: ft.Page, t: Callable[..., str]) -> ft.Control:
    linked = [(row, url) for row in _installed_mods(conn) if (url := _curseforge_url(row))]

    items: list[ft.Control] = []
    if not linked:
        items.append(ft.Text(t("updates.empty_assisted")))
    for row, url in linked:
        items.append(
            ft.Row(
                controls=[
                    ft.Text(row["name"], expand=True),
                    ft.TextButton(
                        content=t("updates.check_on_curseforge_button"),
                        on_click=lambda e, u=url: page.launch_url(u),
                    ),
                ]
            )
        )

    return ft.Container(
        padding=20,
        content=ft.Column(controls=[ft.Text(t("updates.assisted_notice")), *items]),
    )


def _build_direct(
    *,
    config: Config,
    conn: sqlite3.Connection,
    page: ft.Page,
    t: Callable[..., str],
    client: curseforge.CurseForgeClient,
    on_updated: Callable[[], None],
) -> ft.Control:
    results = ft.Column(controls=[])
    # Populated by do_check(), consumed by do_update() — keyed by mod id so
    # the update button doesn't have to re-fetch the file list it just saw.
    latest_by_mod: dict[str, curseforge.CurseForgeFile] = {}

    def do_update(row: sqlite3.Row) -> None:
        latest = latest_by_mod.get(row["id"])
        if latest is None:
            return
        try:
            mod_info = client.get_mod(row["curseforge_id"])
            with tempfile.TemporaryDirectory(prefix="simslink-cf-update-") as tmp:
                downloaded = client.download(row["curseforge_id"], latest.file_id, Path(tmp) / latest.file_name)
                metadata = mod_manager.ModMetadata(
                    curseforge_id=row["curseforge_id"],
                    author=mod_info.author,
                    category=mod_info.category,
                    installed_version=str(latest.file_id),
                    compat_status=curseforge.compat_status(
                        latest.game_version_min, latest.game_version_max, config.game_version
                    ),
                    short_description=mod_info.short_description,
                    thumbnail_url=mod_info.thumbnail_url,
                    links=json.dumps({"curseforge_url": mod_info.curseforge_url})
                    if mod_info.curseforge_url
                    else None,
                    game_version_min=latest.game_version_min,
                    game_version_max=latest.game_version_max,
                    third_party_distribution_allowed=mod_info.third_party_distribution_allowed,
                )
                download_watcher.confirm_replace(
                    downloaded, row["id"], config=config, conn=conn, metadata=metadata
                )
            on_updated()
        except (
            curseforge.CurseForgeError,
            mod_manager.ModManagerError,
            download_watcher.DownloadWatcherError,
        ) as exc:
            results.controls.insert(0, ft.Text(t("updates.update_error", error=str(exc))))
            page.update()

    def _update_row(row: sqlite3.Row) -> ft.Control:
        return ft.Row(
            controls=[
                ft.Text(row["name"], expand=True),
                ft.Text(t("updates.update_available")),
                ft.TextButton(content=t("updates.update_button"), on_click=lambda e, r=row: do_update(r)),
            ]
        )

    def do_check(e: ft.ControlEvent) -> None:
        rows = [row for row in _installed_mods(conn) if row["curseforge_id"] is not None]
        latest_by_mod.clear()
        if not rows:
            results.controls = [ft.Text(t("updates.empty_direct"))]
            page.update()
            return

        entries: list[ft.Control] = []
        for row in rows:
            try:
                files = client.get_files(row["curseforge_id"])
            except curseforge.CurseForgeError as exc:
                entries.append(ft.Text(t("updates.check_error", error=f"{row['name']}: {exc}")))
                continue
            if not files:
                continue
            latest = files[0]
            if str(latest.file_id) != (row["installed_version"] or ""):
                latest_by_mod[row["id"]] = latest
                entries.append(_update_row(row))

        results.controls = entries or [ft.Text(t("updates.no_updates"))]
        page.update()

    return ft.Container(
        padding=20,
        content=ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Text(t("updates.direct_notice"), expand=True),
                        ft.TextButton(content=t("updates.check_button"), on_click=do_check),
                    ]
                ),
                results,
            ]
        ),
    )
