import flet as ft

from i18n import translator
from ui import settings as settings_view

T = translator("en")


def test_build_shows_all_configured_folders(app_config):
    calls: list[str] = []

    control = settings_view.build(
        config=app_config, t=T, current_language="en", on_language_change=calls.append
    )

    texts = {c.value for c in _iter(control, ft.Text)}
    assert str(app_config.sims4_game_dir) in texts
    assert str(app_config.sims4_mods_dir) in texts
    assert str(app_config.library_dir) in texts


def test_language_dropdown_invokes_callback(app_config):
    calls: list[str] = []

    control = settings_view.build(
        config=app_config, t=T, current_language="en", on_language_change=calls.append
    )

    dropdown = _iter(control, ft.Dropdown)[0]

    class FakeEvent:
        control = dropdown

    dropdown.value = "fr"
    dropdown.on_select(FakeEvent())

    assert calls == ["fr"]


def _iter(control, cls):
    results = []
    stack = [control]
    while stack:
        current = stack.pop()
        if isinstance(current, cls):
            results.append(current)
        content = getattr(current, "content", None)
        if content is not None:
            stack.append(content)
        stack.extend(getattr(current, "controls", None) or [])
    return results
