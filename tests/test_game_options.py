from backend import game_options


def test_script_mods_allowed_true(app_config):
    (app_config.sims4_user_dir / "options.ini").write_text(
        "[GraphicsSettings]\nscriptmodsallowed=1\n"
    )

    assert game_options.script_mods_allowed(app_config) is True


def test_script_mods_allowed_false(app_config):
    (app_config.sims4_user_dir / "options.ini").write_text(
        "[GraphicsSettings]\nScriptModsAllowed=False\n"
    )

    assert game_options.script_mods_allowed(app_config) is False


def test_script_mods_allowed_none_when_file_missing(app_config):
    assert game_options.script_mods_allowed(app_config) is None


def test_script_mods_allowed_none_when_key_missing(app_config):
    (app_config.sims4_user_dir / "options.ini").write_text("[GraphicsSettings]\nresolution=1920x1080\n")

    assert game_options.script_mods_allowed(app_config) is None


def test_script_mods_allowed_none_for_unrecognizable_value(app_config):
    (app_config.sims4_user_dir / "options.ini").write_text("scriptmodsallowed=maybe\n")

    assert game_options.script_mods_allowed(app_config) is None


def test_script_mods_allowed_ignores_section_name(app_config):
    # The key is matched regardless of which [Section] it sits under — the
    # real game's section naming isn't documented/guaranteed.
    (app_config.sims4_user_dir / "options.ini").write_text(
        "[SomeOtherSection]\nscriptmodsallowed=true\n"
    )

    assert game_options.script_mods_allowed(app_config) is True


def test_script_mods_allowed_ignores_comments_and_blank_lines(app_config):
    (app_config.sims4_user_dir / "options.ini").write_text(
        "; a comment\n\n[GraphicsSettings]\n\nscriptmodsallowed=1\n"
    )

    assert game_options.script_mods_allowed(app_config) is True
