"""Minimal i18n loader for UI strings.

CLAUDE.md is explicit: no UI string may be hardcoded outside this layer. All
UI-facing text goes through translator(language)'s t(key, **kwargs), which
falls back to English and finally to the key itself for anything missing.

Language selection here is in-memory only for now (Phase 2); a persisted
manual override lands with the full Settings view in a later phase.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

_I18N_DIR = Path(__file__).parent
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "fr")

_cache: dict[str, dict[str, str]] = {}


def _load(language: str) -> dict[str, str]:
    if language not in _cache:
        path = _I18N_DIR / f"{language}.json"
        _cache[language] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )
    return _cache[language]


def translator(language: str) -> Callable[..., str]:
    strings = _load(language)
    fallback = _load(DEFAULT_LANGUAGE)

    def t(key: str, **kwargs) -> str:
        template = strings.get(key) or fallback.get(key, key)
        return template.format(**kwargs) if kwargs else template

    return t


def detect_system_language() -> str:
    for env_var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(env_var, "")
        if value.lower().startswith("fr"):
            return "fr"
    return DEFAULT_LANGUAGE
