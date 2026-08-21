from pathlib import Path

import pytest

import backend.config as config_module
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
        "SIMS4_MODS_DIR": "/home/user/Documents/Electronic Arts/The Sims 4/Mods",
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
    assert "SIMS4_MODS_DIR" in message
    assert "SIMS4_USER_DIR" in message
    assert "LIBRARY_DIR" in message
    assert "SIMS4_GAME_DIR" not in message


def test_from_env_loads_required_values(tmp_path):
    env_path = write_env_file(tmp_path)

    config = Config.from_env(env_path)

    assert config.sims4_game_dir == Path("/games/sims4")
    assert config.library_dir == Path("/home/user/simslink-library")
    assert config.curseforge_api_key is None
    assert config.has_api_key is False


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
