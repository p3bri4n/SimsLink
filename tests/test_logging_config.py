import dataclasses
import logging

from backend import logging_config


def _get_backend_logger() -> logging.Logger:
    logger = logging.getLogger("backend")
    return logger


def test_configure_logging_sets_level(app_config, tmp_path):
    logging_config.configure_logging(app_config, log_path=tmp_path / "simslink.log")

    assert _get_backend_logger().level == logging.INFO


def test_configure_logging_respects_configured_level(app_config, tmp_path):
    debug_config = dataclasses.replace(app_config, log_level="DEBUG")

    logging_config.configure_logging(debug_config, log_path=tmp_path / "simslink.log")

    assert _get_backend_logger().level == logging.DEBUG


def test_configure_logging_creates_log_file_parent_dir(app_config, tmp_path):
    log_path = tmp_path / "nested" / "simslink.log"

    logging_config.configure_logging(app_config, log_path=log_path)

    assert log_path.parent.is_dir()


def test_configure_logging_writes_to_the_log_file(app_config, tmp_path):
    log_path = tmp_path / "simslink.log"
    logging_config.configure_logging(app_config, log_path=log_path)

    logging.getLogger("backend.somewhere").info("hello from a test")

    assert "hello from a test" in log_path.read_text(encoding="utf-8")


def test_configure_logging_is_idempotent_no_duplicate_handlers(app_config, tmp_path):
    log_path = tmp_path / "simslink.log"

    logging_config.configure_logging(app_config, log_path=log_path)
    logging_config.configure_logging(app_config, log_path=log_path)

    assert len(_get_backend_logger().handlers) == 2  # one console + one file, not four


def test_configure_logging_does_not_touch_unrelated_loggers(app_config, tmp_path):
    logging_config.configure_logging(app_config, log_path=tmp_path / "simslink.log")

    assert logging.getLogger("uvicorn").handlers == []
