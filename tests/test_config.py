from pathlib import Path

import pytest

import backend.config as config_module
import backend.db as db_module
import backend.game_options as game_options_module
from backend.config import Config, ConfigError, DEFAULT_DOWNLOAD_WATCH_DIR, detect_symlink_support

ALL_ENV_VARS = (
    "SIMS4_GAME_DIR",
    "SIMS4_MODS_DIR",
    "SIMS4_USER_DIR",
    "LIBRARY_DIR",
    "CURSEFORGE_API_KEY",
    "DOWNLOAD_WATCH_DIR",
    "GAME_VERSION",
    "BACKUP_RETENTION_COUNT",
    "MODS_WATCHER_ENABLED",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def write_env_file(tmp_path: Path, **overrides: str) -> Path:
    values = {
        "SIMS4_GAME_DIR": "/games/sims4",
        "SIMS4_USER_DIR": "/home/user/Documents/Electronic Arts/The Sims 4",
        "LIBRARY_DIR": "/home/user/simslink-library",
        **overrides,
    }
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{k}={v}" for k, v in values.items()))
    return env_path


def test_from_env_missing_required_raises(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SIMS4_GAME_DIR=/games/sims4\n")

    with pytest.raises(ConfigError) as exc_info:
        Config.from_env(env_path)

    message = str(exc_info.value)
    assert "SIMS4_USER_DIR" in message
    assert "SIMS4_GAME_DIR" not in message
    # Neither is a real required var anymore — SIMS4_MODS_DIR is derived
    # from SIMS4_USER_DIR, LIBRARY_DIR defaults to ~/.SimsLink/library.
    assert "SIMS4_MODS_DIR" not in message
    assert "LIBRARY_DIR" not in message


def test_from_env_loads_required_values(tmp_path):
    env_path = write_env_file(tmp_path)

    config = Config.from_env(env_path)

    assert config.sims4_game_dir == Path("/games/sims4")
    assert config.library_dir == Path("/home/user/simslink-library")
    assert config.curseforge_api_key is None
    assert config.has_api_key is False


def test_from_env_sims4_mods_dir_is_derived_from_user_dir(tmp_path):
    env_path = write_env_file(tmp_path)

    config = Config.from_env(env_path)

    assert config.sims4_mods_dir == config.sims4_user_dir / "Mods"


def test_from_env_sims4_mods_dir_env_var_is_ignored_if_still_set(tmp_path):
    # A stale SIMS4_MODS_DIR left over in an old .env from before this was
    # derived automatically must not silently disagree with SIMS4_USER_DIR.
    env_path = write_env_file(tmp_path, SIMS4_MODS_DIR="/somewhere/unrelated")

    config = Config.from_env(env_path)

    assert config.sims4_mods_dir == config.sims4_user_dir / "Mods"


def test_from_env_library_dir_defaults_to_dot_simslink_when_unset(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SIMS4_GAME_DIR=/games/sims4\nSIMS4_USER_DIR=/home/user/Documents/Electronic Arts/The Sims 4\n"
    )

    config = Config.from_env(env_path)

    assert config.library_dir == config_module.DEFAULT_LIBRARY_DIR
    assert config.library_dir == Path.home() / ".SimsLink" / "library"


def test_from_env_game_version_uses_explicit_value_without_detecting(tmp_path, monkeypatch):
    def fail_if_called(game_dir):
        raise AssertionError("detect_game_version should not run when GAME_VERSION is set")

    monkeypatch.setattr(game_options_module, "detect_game_version", fail_if_called)
    env_path = write_env_file(tmp_path, GAME_VERSION="1.100.0.0")

    config = Config.from_env(env_path)

    assert config.game_version == "1.100.0.0"


def test_from_env_game_version_falls_back_to_auto_detection_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(game_options_module, "detect_game_version", lambda game_dir: "1.126.78.1020")
    env_path = write_env_file(tmp_path)

    config = Config.from_env(env_path)

    assert config.game_version == "1.126.78.1020"


def test_from_env_game_version_none_when_undetectable(tmp_path):
    # No GAME_VERSION set and SIMS4_GAME_DIR (from write_env_file) doesn't
    # point at a real game install, so detection has nothing to read.
    env_path = write_env_file(tmp_path)

    config = Config.from_env(env_path)

    assert config.game_version is None


def test_from_env_download_watch_dir_defaults_when_unset(tmp_path):
    env_path = write_env_file(tmp_path)

    config = Config.from_env(env_path)

    assert config.download_watch_dir == DEFAULT_DOWNLOAD_WATCH_DIR


def test_from_env_download_watch_dir_uses_explicit_value(tmp_path):
    env_path = write_env_file(tmp_path, DOWNLOAD_WATCH_DIR="/home/user/mydownloads")

    config = Config.from_env(env_path)

    assert config.download_watch_dir == Path("/home/user/mydownloads")


def test_from_env_backup_retention_count_defaults_when_unset(tmp_path):
    env_path = write_env_file(tmp_path)

    config = Config.from_env(env_path)

    assert config.backup_retention_count == 5


def test_from_env_backup_retention_count_uses_explicit_value(tmp_path):
    env_path = write_env_file(tmp_path, BACKUP_RETENTION_COUNT="10")

    config = Config.from_env(env_path)

    assert config.backup_retention_count == 10


def test_from_env_backup_retention_count_rejects_non_numeric(tmp_path):
    env_path = write_env_file(tmp_path, BACKUP_RETENTION_COUNT="lots")

    with pytest.raises(ConfigError):
        Config.from_env(env_path)


def test_from_env_backup_retention_count_rejects_zero(tmp_path):
    env_path = write_env_file(tmp_path, BACKUP_RETENTION_COUNT="0")

    with pytest.raises(ConfigError):
        Config.from_env(env_path)


def test_from_env_mods_watcher_enabled_defaults_true_when_unset(tmp_path):
    env_path = write_env_file(tmp_path)

    config = Config.from_env(env_path)

    assert config.mods_watcher_enabled is True


def test_from_env_mods_watcher_enabled_can_be_disabled(tmp_path):
    env_path = write_env_file(tmp_path, MODS_WATCHER_ENABLED="false")

    config = Config.from_env(env_path)

    assert config.mods_watcher_enabled is False


def test_from_env_mods_watcher_enabled_accepts_various_truthy_spellings(tmp_path):
    for spelling in ("1", "true", "True", "yes", "on"):
        env_path = write_env_file(tmp_path, MODS_WATCHER_ENABLED=spelling)
        assert Config.from_env(env_path).mods_watcher_enabled is True, spelling


def test_from_env_mods_watcher_enabled_rejects_unrecognized_value(tmp_path):
    env_path = write_env_file(tmp_path, MODS_WATCHER_ENABLED="maybe")

    with pytest.raises(ConfigError):
        Config.from_env(env_path)


def test_from_env_log_level_defaults_to_info(tmp_path):
    env_path = write_env_file(tmp_path)

    config = Config.from_env(env_path)

    assert config.log_level == "INFO"


def test_from_env_log_level_uses_explicit_value(tmp_path):
    env_path = write_env_file(tmp_path, LOG_LEVEL="debug")

    config = Config.from_env(env_path)

    assert config.log_level == "DEBUG"  # normalized to uppercase


def test_from_env_log_level_rejects_unknown_level(tmp_path):
    env_path = write_env_file(tmp_path, LOG_LEVEL="VERBOSE")

    with pytest.raises(ConfigError):
        Config.from_env(env_path)


def test_db_path_and_log_path_live_under_dot_simslink_by_default(tmp_path):
    env_path = write_env_file(tmp_path)

    config = Config.from_env(env_path)

    assert config.db_path == Path.home() / ".SimsLink" / "simslink.sqlite3"
    assert config.log_path == Path.home() / ".SimsLink" / "simslink.log"


def test_log_path_is_next_to_db_path(tmp_path):
    env_path = write_env_file(tmp_path)

    config = Config.from_env(env_path)

    assert config.log_path.parent == config.db_path.parent
    assert config.log_path.name == "simslink.log"


def test_has_api_key_true_for_non_blank_value(tmp_path):
    env_path = write_env_file(tmp_path, CURSEFORGE_API_KEY="a-real-key")

    config = Config.from_env(env_path)

    assert config.has_api_key is True


def test_has_api_key_false_for_whitespace_only_value(tmp_path):
    env_path = write_env_file(tmp_path, CURSEFORGE_API_KEY="   ")

    config = Config.from_env(env_path)

    assert config.has_api_key is False


def test_real_env_var_overrides_dotenv_file(tmp_path, monkeypatch):
    env_path = write_env_file(tmp_path, LIBRARY_DIR="/from/dotenv")
    monkeypatch.setenv("LIBRARY_DIR", "/from/real/environment")

    config = Config.from_env(env_path)

    assert config.library_dir == Path("/from/real/environment")


def test_regression_default_env_path_resolves_to_project_root_not_backend_dir():
    """config.py lives in backend/, so a plain Path(__file__).parent (rather
    than .parent.parent) would silently resolve DEFAULT_ENV_PATH to
    backend/.env instead of the project root's .env — the real .env file
    would then never be found by Config.from_env() with no explicit path."""
    project_root = Path(config_module.__file__).resolve().parent.parent

    assert config_module.DEFAULT_ENV_PATH == project_root / ".env"
    # Ground-truth check independent of how many .parent hops the fix uses:
    # the resolved directory must actually be the project root (pyproject.toml
    # lives there), not backend/.
    assert (config_module.DEFAULT_ENV_PATH.parent / "pyproject.toml").is_file()


def test_detect_symlink_support_true_on_normal_filesystem(tmp_path):
    assert detect_symlink_support(tmp_path) is True


def test_detect_symlink_support_false_when_symlink_raises(tmp_path, monkeypatch):
    def raise_oserror(self, target):
        raise OSError("symlinks not supported")

    monkeypatch.setattr(Path, "symlink_to", raise_oserror)

    assert detect_symlink_support(tmp_path) is False


# --- migrate_legacy_data_dir --------------------------------------------------------
# Regression coverage for the incident this fixed: DEFAULT_DATA_DIR moved from
# the XDG data dir to ~/.SimsLink/ without migrating an existing install's
# real database, so every installed mod silently vanished from the Library
# (broken-folder detection kept working since it reads Mods/ directly, which
# is what made the symptom so confusing). LEGACY_DATA_DIR/DEFAULT_DATA_DIR are
# monkeypatched to tmp_path locations in every test here — this suite must
# never touch the developer's real ~/.SimsLink or real XDG data dir.


def _init_db(path: Path, *, with_mod: bool) -> None:
    conn = db_module.init_db(path)
    if with_mod:
        conn.execute(
            "INSERT INTO mods (id, name, library_path, primary_type, install_date) VALUES (?, ?, ?, ?, ?)",
            ("mod1", "Some Mod", "/lib/mod1", "package", "2026-08-22T00:00:00+00:00"),
        )
        conn.commit()
    conn.close()


def test_migrate_legacy_data_dir_moves_populated_db(tmp_path, monkeypatch):
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    monkeypatch.setattr(config_module, "LEGACY_DATA_DIR", old_dir)
    monkeypatch.setattr(config_module, "DEFAULT_DATA_DIR", new_dir)
    _init_db(old_dir / "simslink.sqlite3", with_mod=True)
    (old_dir / "simslink.log").write_text("old log")

    config_module.migrate_legacy_data_dir()

    assert (new_dir / "simslink.log").read_text() == "old log"
    assert not (old_dir / "simslink.sqlite3").exists()
    assert config_module._sqlite_has_any_mods(new_dir / "simslink.sqlite3") is True


def test_migrate_legacy_data_dir_does_nothing_when_old_db_has_no_mods(tmp_path, monkeypatch):
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    monkeypatch.setattr(config_module, "LEGACY_DATA_DIR", old_dir)
    monkeypatch.setattr(config_module, "DEFAULT_DATA_DIR", new_dir)
    _init_db(old_dir / "simslink.sqlite3", with_mod=False)

    config_module.migrate_legacy_data_dir()

    assert not new_dir.exists()
    assert (old_dir / "simslink.sqlite3").exists()


def test_migrate_legacy_data_dir_never_overwrites_new_locations_own_data(tmp_path, monkeypatch):
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    monkeypatch.setattr(config_module, "LEGACY_DATA_DIR", old_dir)
    monkeypatch.setattr(config_module, "DEFAULT_DATA_DIR", new_dir)
    _init_db(old_dir / "simslink.sqlite3", with_mod=True)
    _init_db(new_dir / "simslink.sqlite3", with_mod=True)

    config_module.migrate_legacy_data_dir()

    # Both untouched — the new location already had real data of its own.
    assert (old_dir / "simslink.sqlite3").exists()
    assert (new_dir / "simslink.sqlite3").exists()


def test_migrate_legacy_data_dir_noop_when_no_legacy_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "LEGACY_DATA_DIR", tmp_path / "old")
    monkeypatch.setattr(config_module, "DEFAULT_DATA_DIR", tmp_path / "new")

    config_module.migrate_legacy_data_dir()  # must not raise

    assert not (tmp_path / "new").exists()
