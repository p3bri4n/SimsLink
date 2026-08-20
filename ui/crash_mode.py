"""Crash Mode view: automated suspect analysis, guided bisection, and cache
cleanup. Never deletes or disables a mod on its own — every state change
(bisection round toggles aside, which are reversible and reported) requires
an explicit user action."""

from __future__ import annotations

import sqlite3
from typing import Callable

import flet as ft

import cache_cleaner
import crash_analyzer
from config import Config


def build(*, config: Config, conn: sqlite3.Connection, page: ft.Page, t: Callable[..., str]) -> ft.Control:
    status = ft.Column(controls=[])

    def mod_name(mod_id: str) -> str:
        row = conn.execute("SELECT name FROM mods WHERE id = ?", (mod_id,)).fetchone()
        return row["name"] if row is not None else mod_id

    def run_analysis(e: ft.ControlEvent) -> None:
        exception_path = config.sims4_user_dir / "lastException.txt"
        if not exception_path.is_file():
            status.controls = [ft.Text(t("crash.no_exception_file"))]
            page.update()
            return
        raw = exception_path.read_text(encoding="utf-8", errors="replace")
        crash_log_id = crash_analyzer.record_crash(raw, conn=conn)
        _render_suspects(crash_log_id)

    def _render_suspects(crash_log_id: int) -> None:
        suspects = crash_analyzer.get_suspects(crash_log_id, conn)
        if suspects:
            status.controls = [
                ft.Text(t("crash.suspects_heading"), weight=ft.FontWeight.BOLD),
                *[
                    ft.Text(
                        t(
                            "crash.suspect_line",
                            name=mod_name(s.mod_id),
                            confidence=s.confidence,
                            reason=s.reason,
                        )
                    )
                    for s in suspects
                ],
            ]
        else:
            status.controls = [
                ft.Text(t("crash.no_suspects")),
                ft.TextButton(
                    content=t("crash.start_bisection"),
                    on_click=lambda e: _start_bisection(crash_log_id),
                ),
            ]
        page.update()

    def _start_bisection(crash_log_id: int) -> None:
        try:
            disabled = crash_analyzer.start_bisection(crash_log_id, config=config, conn=conn)
        except crash_analyzer.CrashAnalyzerError as exc:
            status.controls = [ft.Text(str(exc))]
            page.update()
            return
        _render_bisection_round(crash_log_id, disabled)

    def _render_bisection_round(crash_log_id: int, disabled: list[str]) -> None:
        names = ", ".join(mod_name(m) for m in disabled)
        status.controls = [
            ft.Text(t("crash.bisection_round", mods=names)),
            ft.Row(
                controls=[
                    ft.TextButton(
                        content=t("crash.bisection_still_crashes"),
                        on_click=lambda e: _report(crash_log_id, True),
                    ),
                    ft.TextButton(
                        content=t("crash.bisection_fixed"),
                        on_click=lambda e: _report(crash_log_id, False),
                    ),
                ]
            ),
        ]
        page.update()

    def _report(crash_log_id: int, crash_occurred: bool) -> None:
        result = crash_analyzer.report_bisection_result(
            crash_log_id, crash_occurred, config=config, conn=conn
        )
        if isinstance(result, list):
            _render_bisection_round(crash_log_id, result)
        elif result is not None:
            status.controls = [
                ft.Text(t("crash.bisection_converged", name=mod_name(result))),
                ft.TextButton(
                    content=t("crash.confirm_faulty"),
                    on_click=lambda e: _confirm_faulty(crash_log_id, result),
                ),
            ]
            page.update()
        else:
            status.controls = [ft.Text(t("crash.bisection_inconclusive"))]
            page.update()

    def _confirm_faulty(crash_log_id: int, mod_id: str) -> None:
        crash_analyzer.confirm_faulty_mod(crash_log_id, mod_id, conn)
        status.controls = [ft.Text(t("crash.faulty_confirmed", name=mod_name(mod_id)))]
        page.update()

    def clear_cache(e: ft.ControlEvent) -> None:
        existing = [target for target in cache_cleaner.list_cache_targets(config) if target.exists]

        def do_clean(e: ft.ControlEvent) -> None:
            cache_cleaner.clean_cache(config)
            page.pop_dialog()

        dialog = ft.AlertDialog(
            title=ft.Text(t("crash.clear_cache_title")),
            content=ft.Column(
                controls=[ft.Text(f"{target.name}: {target.description}") for target in existing]
                or [ft.Text(t("crash.clear_cache_nothing"))]
            ),
            actions=[
                ft.TextButton(content=t("crash.clear_cache_cancel"), on_click=lambda e: page.pop_dialog()),
                ft.TextButton(content=t("crash.clear_cache_confirm"), on_click=do_clean),
            ],
        )
        page.show_dialog(dialog)

    return ft.Container(
        padding=20,
        content=ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Button(content=t("crash.analyze_button"), on_click=run_analysis),
                        ft.TextButton(content=t("crash.clear_cache_button"), on_click=clear_cache),
                    ]
                ),
                status,
            ]
        ),
    )
