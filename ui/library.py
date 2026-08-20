"""Library view: installed mods grid + detail/delete dialogs."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

import flet as ft

import mod_manager
from config import Config


@dataclass
class LibraryView:
    control: ft.Control
    refresh: Callable[[], None]


def build(*, config: Config, conn: sqlite3.Connection, page: ft.Page, t: Callable[..., str]) -> LibraryView:
    grid = ft.GridView(max_extent=220, child_aspect_ratio=0.85, spacing=10, run_spacing=10, expand=True)
    container = ft.Container(content=grid, padding=10, expand=True)

    def refresh() -> None:
        grid.controls.clear()
        rows = conn.execute("SELECT * FROM mods ORDER BY name COLLATE NOCASE").fetchall()
        if not rows:
            grid.controls.append(ft.Text(t("library.empty")))
        for row in rows:
            grid.controls.append(_build_card(row, config=config, conn=conn, page=page, t=t, on_change=refresh))
        page.update()

    refresh()
    return LibraryView(control=container, refresh=refresh)


def _build_card(
    row: sqlite3.Row,
    *,
    config: Config,
    conn: sqlite3.Connection,
    page: ft.Page,
    t: Callable[..., str],
    on_change: Callable[[], None],
) -> ft.Control:
    is_active = bool(row["active"])
    return ft.Card(
        content=ft.Container(
            padding=10,
            content=ft.Column(
                controls=[
                    ft.Text(row["name"], weight=ft.FontWeight.BOLD, opacity=1.0 if is_active else 0.4),
                    ft.Text(row["short_description"] or "", size=11, max_lines=2),
                    ft.Row(
                        controls=[
                            ft.Switch(
                                value=is_active,
                                tooltip=t("library.toggle_tooltip"),
                                on_change=lambda e, mod_id=row["id"]: _toggle(
                                    mod_id, e.control.value, config=config, conn=conn, on_change=on_change
                                ),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.INFO_OUTLINE,
                                tooltip=t("library.details_tooltip"),
                                on_click=lambda e, mod_id=row["id"]: _show_detail(
                                    mod_id, config=config, conn=conn, page=page, t=t
                                ),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                tooltip=t("library.delete_tooltip"),
                                on_click=lambda e, mod_id=row["id"]: _confirm_delete(
                                    mod_id, config=config, conn=conn, page=page, t=t, on_change=on_change
                                ),
                            ),
                        ],
                    ),
                ],
            ),
        ),
    )


def _toggle(mod_id: str, active: bool, *, config: Config, conn: sqlite3.Connection, on_change: Callable[[], None]) -> None:
    if active:
        mod_manager.enable(mod_id, config=config, conn=conn)
    else:
        mod_manager.disable(mod_id, config=config, conn=conn)
    on_change()


def _show_detail(
    mod_id: str, *, config: Config, conn: sqlite3.Connection, page: ft.Page, t: Callable[..., str]
) -> None:
    row = conn.execute("SELECT * FROM mods WHERE id = ?", (mod_id,)).fetchone()
    files = conn.execute(
        "SELECT relative_path FROM mod_files WHERE mod_id = ? ORDER BY relative_path", (mod_id,)
    ).fetchall()
    unknown = t("library.unknown")

    dialog = ft.AlertDialog(
        title=ft.Text(row["name"]),
        content=ft.Column(
            controls=[
                ft.Text(row["full_description"] or row["short_description"] or t("library.detail.no_description")),
                ft.Text(t("library.detail.author", value=row["author"] or unknown)),
                ft.Text(t("library.detail.category", value=row["category"] or unknown)),
                ft.Text(t("library.detail.installed_version", value=row["installed_version"] or unknown)),
                ft.Text(t("library.detail.compatibility", value=row["compat_status"])),
                ft.Divider(),
                ft.Text(t("library.detail.files_heading"), weight=ft.FontWeight.BOLD),
                *[ft.Text(f["relative_path"], size=11) for f in files],
            ],
            scroll=ft.ScrollMode.AUTO,
            tight=True,
        ),
        actions=[ft.TextButton(content=t("library.detail.close"), on_click=lambda e: page.pop_dialog())],
    )
    page.show_dialog(dialog)


def _confirm_delete(
    mod_id: str,
    *,
    config: Config,
    conn: sqlite3.Connection,
    page: ft.Page,
    t: Callable[..., str],
    on_change: Callable[[], None],
) -> None:
    row = conn.execute("SELECT name FROM mods WHERE id = ?", (mod_id,)).fetchone()

    def do_delete(e) -> None:
        mod_manager.delete(mod_id, config=config, conn=conn)
        page.pop_dialog()
        on_change()

    dialog = ft.AlertDialog(
        title=ft.Text(t("library.delete_confirm.title")),
        content=ft.Text(t("library.delete_confirm.message", name=row["name"])),
        actions=[
            ft.TextButton(content=t("library.delete_confirm.cancel"), on_click=lambda e: page.pop_dialog()),
            ft.TextButton(content=t("library.delete_confirm.confirm"), on_click=do_delete),
        ],
    )
    page.show_dialog(dialog)
