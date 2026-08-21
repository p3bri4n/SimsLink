"""Reads the game's options.ini to check settings SimsLink cares about.

The Sims 4's options.ini format isn't officially documented, and section
names have been observed to vary across game versions/platforms. Rather than
assume a specific `[SectionName]` (a wrong guess there would make the check
silently never find anything), this scans every "key=value" line in the file
regardless of section, matching the key case-insensitively.

Currently checks one thing: "Script Mods Allowed" — without it, no
.ts4script file loads at all, and the game gives no error message when that
happens, making it a common source of "my script mods don't work" confusion.

The actual ini key the game writes is `scriptmodsenabled`, not
`scriptmodsallowed` — the latter was an initial guess based on the setting's
display name in-game, confirmed wrong against a real options.ini. Function/
route naming keeps "allowed" since that's the concept surfaced to the user
(matches the in-game setting's label); only the key string searched for in
the file changes.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config

_KV_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

SCRIPT_MODS_ALLOWED_KEY = "scriptmodsenabled"


def _find_key(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith(("[", ";", "#")):
            continue
        match = _KV_RE.match(line)
        if match and match.group(1).lower() == key.lower():
            return match.group(2)
    return None


def script_mods_allowed(config: Config) -> bool | None:
    """True/False if the setting is present and its value is an
    unambiguous boolean; None if options.ini doesn't exist yet (e.g. the
    game has never been launched) or the key/value isn't recognizable —
    callers should treat None as "unknown", not "disabled"."""
    value = _find_key(config.sims4_user_dir / "options.ini", SCRIPT_MODS_ALLOWED_KEY)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    return None
