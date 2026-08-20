"""Settings view. Minimal folder/language display lands in Phase 2 — no
persistence layer yet, so the language selector only affects the running
session. The full editable settings set (§6.8 of the brief) is Phase 6."""

from __future__ import annotations

from typing import Callable

import flet as ft

from config import Config


def build(
    *, config: Config, t: Callable[..., str], current_language: str, on_language_change: Callable[[str], None]
) -> ft.Control:
    return ft.Container(
        padding=20,
        content=ft.Column(
            controls=[
                ft.Text(t("settings.folders_heading"), weight=ft.FontWeight.BOLD, size=16),
                _folder_row(t("settings.folder.game_dir"), config.sims4_game_dir),
                _folder_row(t("settings.folder.mods_dir"), config.sims4_mods_dir),
                _folder_row(t("settings.folder.user_dir"), config.sims4_user_dir),
                _folder_row(t("settings.folder.library_dir"), config.library_dir),
                _folder_row(t("settings.folder.download_watch_dir"), config.download_watch_dir),
                ft.Divider(),
                ft.Text(t("settings.language_heading"), weight=ft.FontWeight.BOLD, size=16),
                ft.Dropdown(
                    value=current_language,
                    width=250,
                    options=[
                        ft.DropdownOption(key="en", text=t("settings.language.english")),
                        ft.DropdownOption(key="fr", text=t("settings.language.french")),
                    ],
                    on_select=lambda e: on_language_change(e.control.value),
                ),
            ],
            spacing=10,
        ),
    )


def _folder_row(label: str, path) -> ft.Control:
    return ft.Row(controls=[ft.Text(label, width=220), ft.Text(str(path), selectable=True)])
