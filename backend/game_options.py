"""Reads game-side files to surface information SimsLink cares about:
options.ini settings, and (see detect_game_version) the installed build
version.

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

import pefile

from .config import Config

_KV_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

SCRIPT_MODS_ALLOWED_KEY = "scriptmodsenabled"


def _resolve_ini_path(path: Path) -> Path | None:
    """Resolves options.ini's actual on-disk name case-insensitively.

    The real file has been observed as `Options.ini` (capital O), not the
    lowercase `options.ini` this module used to check for. Windows/Wine
    filesystems don't care about the difference, but this runs on Linux
    (case-sensitive), so an exact-case check silently never finds the file.
    """
    if path.is_file():
        return path
    if not path.parent.is_dir():
        return None
    target = path.name.lower()
    for entry in path.parent.iterdir():
        if entry.is_file() and entry.name.lower() == target:
            return entry
    return None


def _find_key(path: Path, key: str) -> str | None:
    resolved = _resolve_ini_path(path)
    if resolved is None:
        return None
    path = resolved
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


_GAME_EXE_NAMES = ("TS4_x64.exe", "TS4_DX9_x64.exe")


def detect_game_version(game_dir: Path) -> str | None:
    """Reads the installed build version (e.g. "1.126.78.1020") from
    Game/Bin/TS4_x64.exe's embedded Windows PE VERSIONINFO resource — the
    same string EA's launcher and community mod tools display, and what
    CurseForge's gameVersion filtering keys off of.

    Works against a Wine/Proton-installed .exe just as well as a native
    Windows one since this only reads raw bytes from the file directly
    (via pefile) — no Windows API involved. Falls back to
    TS4_DX9_x64.exe (the older DirectX 9 renderer, still shipped
    alongside the main one) if the primary exe isn't present.

    Best-effort only: returns None on anything unexpected (exe missing,
    unreadable, no version resource) rather than raising. GAME_VERSION in
    .env always takes precedence over this — see config.py."""
    exe_path = next(
        (game_dir / "Game" / "Bin" / name for name in _GAME_EXE_NAMES if (game_dir / "Game" / "Bin" / name).is_file()),
        None,
    )
    if exe_path is None:
        return None

    try:
        pe = pefile.PE(str(exe_path), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        for file_info in getattr(pe, "FileInfo", []):
            for entry in file_info:
                for string_table in getattr(entry, "StringTable", []):
                    for key, value in string_table.entries.items():
                        if key == b"ProductVersion":
                            version = value.decode("ascii", errors="replace").strip()
                            return version or None
    except Exception:
        return None
    return None
