"""SimsLink desktop entry point."""

from __future__ import annotations

import logging

import flet as ft

import db
import download_watcher
import i18n
import scanner
from config import Config, ConfigError
from ui import library as library_view
from ui import settings as settings_view

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("simslink")


def main(page: ft.Page) -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        t = i18n.translator(i18n.detect_system_language())
        page.title = "SimsLink"
        page.add(ft.Text(t("app.config_error", message=str(exc)), color=ft.Colors.RED))
        return

    page.window.width = 1100
    page.window.height = 720

    conn = db.init_db(config.db_path)
    scanner.import_untracked_mods(config, conn)
    scanner.incremental_scan(config, conn)

    state = {"language": i18n.detect_system_language()}
    body = ft.Container(expand=True)

    def rebuild() -> None:
        t = i18n.translator(state["language"])
        page.title = t("app.title")

        banner = ft.Container(
            content=ft.Text(t("mode.direct_banner") if config.has_api_key else t("mode.assisted_banner")),
            bgcolor=ft.Colors.GREEN_100 if config.has_api_key else ft.Colors.AMBER_100,
            padding=10,
        )

        library = library_view.build(config=config, conn=conn, page=page, t=t)
        settings_control = settings_view.build(
            config=config, t=t, current_language=state["language"], on_language_change=set_language
        )
        content_area = ft.Container(content=library.control, expand=True)

        def show(index: int) -> None:
            content_area.content = library.control if index == 0 else settings_control
            page.update()

        nav = ft.Row(
            controls=[
                ft.TextButton(content=t("nav.library"), on_click=lambda e: show(0)),
                ft.TextButton(content=t("nav.settings"), on_click=lambda e: show(1)),
            ]
        )

        body.content = ft.Column(controls=[banner, nav, content_area], expand=True)
        page.update()

    def set_language(language: str) -> None:
        state["language"] = language
        rebuild()

    rebuild()
    page.add(body)

    async def _dispatch_download(path) -> None:
        # match_existing_mod() touches conn, so it must run here (on the
        # page's own thread/loop), not on the watcher's background thread.
        candidate_mod_id, candidate_mod_name = download_watcher.match_existing_mod(path, conn)
        t = i18n.translator(state["language"])
        _show_download_dialog(
            path,
            candidate_mod_id,
            candidate_mod_name,
            config=config,
            conn=conn,
            page=page,
            t=t,
            on_installed=rebuild,
        )

    def on_download_detected(path) -> None:
        # Runs on the watchdog observer's own thread — must not touch the UI
        # or `conn` directly; hand off to the page's event loop instead.
        page.run_task(_dispatch_download, path)

    watcher = download_watcher.DownloadWatcher(config, on_download_detected)
    watcher.start()

    def on_disconnect(e: ft.ControlEvent) -> None:
        watcher.stop()
        conn.close()

    page.on_disconnect = on_disconnect


def _show_download_dialog(
    path,
    candidate_mod_id,
    candidate_mod_name,
    *,
    config: Config,
    conn,
    page: ft.Page,
    t,
    on_installed,
) -> None:
    if candidate_mod_id is not None:
        message = t("download.detected_replace_message", filename=path.name, mod_name=candidate_mod_name)
    else:
        message = t("download.detected_message", filename=path.name)

    def do_install(e: ft.ControlEvent) -> None:
        download_watcher.confirm_install(path, config=config, conn=conn)
        page.pop_dialog()
        on_installed()

    def do_replace(e: ft.ControlEvent) -> None:
        download_watcher.confirm_replace(path, candidate_mod_id, config=config, conn=conn)
        page.pop_dialog()
        on_installed()

    actions = [ft.TextButton(content=t("download.dismiss_button"), on_click=lambda e: page.pop_dialog())]
    if candidate_mod_id is not None:
        actions.append(ft.TextButton(content=t("download.replace_button"), on_click=do_replace))
    actions.append(ft.TextButton(content=t("download.install_button"), on_click=do_install))

    dialog = ft.AlertDialog(
        title=ft.Text(t("download.detected_title")), content=ft.Text(message), actions=actions
    )
    page.show_dialog(dialog)


if __name__ == "__main__":
    ft.run(main)
