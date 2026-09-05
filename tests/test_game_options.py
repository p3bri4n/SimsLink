import pefile

from backend import game_options


def test_script_mods_allowed_true(app_config):
    (app_config.sims4_user_dir / "options.ini").write_text(
        "[GraphicsSettings]\nscriptmodsenabled=1\n"
    )

    assert game_options.script_mods_allowed(app_config) is True


def test_script_mods_allowed_false(app_config):
    (app_config.sims4_user_dir / "options.ini").write_text(
        "[GraphicsSettings]\nScriptModsEnabled=False\n"
    )

    assert game_options.script_mods_allowed(app_config) is False


def test_script_mods_allowed_none_when_file_missing(app_config):
    assert game_options.script_mods_allowed(app_config) is None


def test_script_mods_allowed_none_when_key_missing(app_config):
    (app_config.sims4_user_dir / "options.ini").write_text("[GraphicsSettings]\nresolution=1920x1080\n")

    assert game_options.script_mods_allowed(app_config) is None


def test_script_mods_allowed_none_for_unrecognizable_value(app_config):
    (app_config.sims4_user_dir / "options.ini").write_text("scriptmodsenabled=maybe\n")

    assert game_options.script_mods_allowed(app_config) is None


def test_script_mods_allowed_ignores_section_name(app_config):
    # The key is matched regardless of which [Section] it sits under — the
    # real game's section naming isn't documented/guaranteed.
    (app_config.sims4_user_dir / "options.ini").write_text(
        "[SomeOtherSection]\nscriptmodsenabled=true\n"
    )

    assert game_options.script_mods_allowed(app_config) is True


def test_script_mods_allowed_ignores_comments_and_blank_lines(app_config):
    (app_config.sims4_user_dir / "options.ini").write_text(
        "; a comment\n\n[GraphicsSettings]\n\nscriptmodsenabled=1\n"
    )

    assert game_options.script_mods_allowed(app_config) is True


def test_regression_key_is_scriptmodsenabled_not_scriptmodsallowed(app_config):
    # SCRIPT_MODS_ALLOWED_KEY originally searched for "scriptmodsallowed", a
    # guess based on the in-game setting's display name. A real options.ini
    # confirmed the actual key the game writes is "scriptmodsenabled" — the
    # old guess left the check permanently returning None (unknown) against
    # a real file, even with the setting explicitly present.
    (app_config.sims4_user_dir / "options.ini").write_text("scriptmodsallowed=1\n")

    assert game_options.script_mods_allowed(app_config) is None


def test_regression_finds_options_ini_with_capital_o(app_config):
    # The real file has been observed on disk as "Options.ini" (capital O),
    # not "options.ini". On a case-sensitive filesystem (Linux), an
    # exact-case lookup silently never finds it, permanently returning None
    # ("unknown") even though the file and key are both present.
    (app_config.sims4_user_dir / "Options.ini").write_text("scriptmodsenabled=1\n")

    assert game_options.script_mods_allowed(app_config) is True


# --- detect_game_version -----------------------------------------------------
#
# Building a byte-perfect synthetic PE executable (DOS header, section table,
# resource directory tree) isn't worth it here — pefile's own test suite
# already covers PE/VERSIONINFO parsing correctness. These tests fake
# pefile.PE's result shape instead, covering detect_game_version's own logic:
# locating the right exe, extracting ProductVersion, and failing safely.


class _FakeStringTable:
    def __init__(self, entries):
        self.entries = entries


class _FakeFileInfoEntry:
    def __init__(self, string_tables):
        self.StringTable = string_tables


class _FakePE:
    def __init__(self, file_info):
        self.FileInfo = file_info

    def parse_data_directories(self, directories=None):
        pass


def _fake_pe_with_product_version(version: bytes):
    string_table = _FakeStringTable({b"ProductVersion": version, b"FileVersion": version})
    return _FakePE([[_FakeFileInfoEntry([string_table])]])


def test_detect_game_version_none_when_exe_missing(tmp_path):
    assert game_options.detect_game_version(tmp_path) is None


def test_detect_game_version_extracts_product_version(app_config, monkeypatch):
    exe_path = app_config.sims4_game_dir / "Game" / "Bin" / "TS4_x64.exe"
    exe_path.parent.mkdir(parents=True)
    exe_path.write_bytes(b"not-a-real-pe")
    monkeypatch.setattr(pefile, "PE", lambda path, fast_load=True: _fake_pe_with_product_version(b"1.126.78.1020"))

    assert game_options.detect_game_version(app_config.sims4_game_dir) == "1.126.78.1020"


def test_detect_game_version_falls_back_to_dx9_exe(app_config, monkeypatch):
    exe_path = app_config.sims4_game_dir / "Game" / "Bin" / "TS4_DX9_x64.exe"
    exe_path.parent.mkdir(parents=True)
    exe_path.write_bytes(b"not-a-real-pe")
    monkeypatch.setattr(pefile, "PE", lambda path, fast_load=True: _fake_pe_with_product_version(b"1.100.0.0"))

    assert game_options.detect_game_version(app_config.sims4_game_dir) == "1.100.0.0"


def test_detect_game_version_none_when_no_version_resource(app_config, monkeypatch):
    exe_path = app_config.sims4_game_dir / "Game" / "Bin" / "TS4_x64.exe"
    exe_path.parent.mkdir(parents=True)
    exe_path.write_bytes(b"not-a-real-pe")
    monkeypatch.setattr(pefile, "PE", lambda path, fast_load=True: _FakePE([]))

    assert game_options.detect_game_version(app_config.sims4_game_dir) is None


def test_detect_game_version_none_when_pefile_raises(app_config, monkeypatch):
    exe_path = app_config.sims4_game_dir / "Game" / "Bin" / "TS4_x64.exe"
    exe_path.parent.mkdir(parents=True)
    exe_path.write_bytes(b"not-a-real-pe")

    def raise_format_error(path, fast_load=True):
        raise pefile.PEFormatError("not a PE file")

    monkeypatch.setattr(pefile, "PE", raise_format_error)

    assert game_options.detect_game_version(app_config.sims4_game_dir) is None
