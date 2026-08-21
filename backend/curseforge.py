"""CurseForge API client (Direct Mode only).

The only module allowed to require CURSEFORGE_API_KEY. Nothing on the
Assisted Mode code path imports or calls this module (see download_watcher.py
and brief section 4bis).

This has never been exercised against the real API: per CLAUDE.md's "Current
project status", no key has been approved yet (application pending at
console.curseforge.com). The endpoint paths below follow CurseForge's
published REST API structure, but exact JSON field names may need
adjustment once real responses are available — _parse_mod()/_parse_file()
are isolated specifically so that fixing a field name later doesn't ripple
through the rest of the client. The game's numeric CurseForge id is likewise
never hardcoded (a guessed id that's wrong would silently return empty
results) — game_id() resolves it once by name via /games and caches it.

Never make an automated request to any CurseForge endpoint without a valid
API key — this includes CDN file downloads, which have required a key since
July 16, 2026 (see CLAUDE.md's "Things to never do").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import requests

DEFAULT_BASE_URL = "https://api.curseforge.com/v1"
_SIMS4_GAME_NAME = "The Sims 4"
_REQUEST_TIMEOUT = 15
_DOWNLOAD_TIMEOUT = 30
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024

_RELEASE_TYPES = {1: "release", 2: "beta", 3: "alpha"}


class CurseForgeError(Exception):
    pass


class CurseForgeAuthError(CurseForgeError):
    """The configured key was rejected (missing/invalid/expired)."""


@dataclass(frozen=True)
class CurseForgeFile:
    file_id: int
    file_name: str
    download_url: str | None
    game_version_min: str | None
    game_version_max: str | None
    release_type: str


@dataclass(frozen=True)
class CurseForgeMod:
    mod_id: int
    name: str
    author: str | None
    category: str | None
    short_description: str
    thumbnail_url: str | None
    curseforge_url: str | None
    third_party_distribution_allowed: bool


def compat_status(
    game_version_min: str | None, game_version_max: str | None, current_version: str | None
) -> str:
    """'compatible' | 'incompatible' | 'unknown' — badge classification.
    Never hides a mod on 'unknown', just marks it (brief section 6.2)."""
    if not current_version or not (game_version_min or game_version_max):
        return "unknown"
    try:
        current = _version_tuple(current_version)
        if game_version_min and current < _version_tuple(game_version_min):
            return "incompatible"
        if game_version_max and current > _version_tuple(game_version_max):
            return "incompatible"
        return "compatible"
    except ValueError:
        return "unknown"


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _parse_mod(data: dict) -> CurseForgeMod:
    authors = data.get("authors") or []
    categories = data.get("categories") or []
    return CurseForgeMod(
        mod_id=data["id"],
        name=data.get("name", ""),
        author=authors[0]["name"] if authors else None,
        category=categories[0]["name"] if categories else None,
        short_description=data.get("summary", ""),
        thumbnail_url=(data.get("logo") or {}).get("thumbnailUrl"),
        curseforge_url=(data.get("links") or {}).get("websiteUrl"),
        # Absent/None is treated as allowed — most mods do; a wrong default
        # here only costs an extra "Open on CurseForge" fallback, not a
        # blocked download of something that was actually fine.
        third_party_distribution_allowed=data.get("allowModDistribution") is not False,
    )


def _parse_file(data: dict) -> CurseForgeFile:
    game_versions = data.get("gameVersions") or []
    return CurseForgeFile(
        file_id=data["id"],
        file_name=data.get("fileName", ""),
        download_url=data.get("downloadUrl"),
        game_version_min=min(game_versions) if game_versions else None,
        game_version_max=max(game_versions) if game_versions else None,
        release_type=_RELEASE_TYPES.get(data.get("releaseType"), "unknown"),
    )


class CurseForgeClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise CurseForgeError("A CurseForge API key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._game_id: int | None = None

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self._base_url}{path}"
        headers = {"x-api-key": self._api_key, "Accept": "application/json"}
        response = self._session.request(method, url, headers=headers, timeout=_REQUEST_TIMEOUT, **kwargs)
        if response.status_code in (401, 403):
            raise CurseForgeAuthError(f"CurseForge API rejected the key (HTTP {response.status_code})")
        response.raise_for_status()
        return response.json()

    def verify_key(self) -> bool:
        """Lightweight validity check, used to decide Direct vs Assisted
        Mode. Any non-auth failure (offline, timeout, ...) is treated as
        'not verified' rather than raising — the banner falls back to
        Assisted Mode instead of the app failing to start."""
        try:
            self._request("GET", "/games")
            return True
        except CurseForgeAuthError:
            return False
        except requests.RequestException:
            return False

    def game_id(self) -> int:
        if self._game_id is None:
            payload = self._request("GET", "/games")
            for game in payload.get("data", []):
                if game.get("name", "").lower() == _SIMS4_GAME_NAME.lower():
                    self._game_id = game["id"]
                    break
            else:
                raise CurseForgeError(f"Could not find '{_SIMS4_GAME_NAME}' in the games list")
        return self._game_id

    def search_mods(
        self, query: str, *, game_version: str | None = None, page_size: int = 20
    ) -> list[CurseForgeMod]:
        params = {"gameId": self.game_id(), "searchFilter": query, "pageSize": page_size}
        if game_version:
            params["gameVersion"] = game_version
        payload = self._request("GET", "/mods/search", params=params)
        return [_parse_mod(item) for item in payload.get("data", [])]

    def get_mod(self, mod_id: int) -> CurseForgeMod:
        payload = self._request("GET", f"/mods/{mod_id}")
        return _parse_mod(payload["data"])

    def get_files(self, mod_id: int) -> list[CurseForgeFile]:
        payload = self._request("GET", f"/mods/{mod_id}/files")
        return [_parse_file(item) for item in payload.get("data", [])]

    def get_description(self, mod_id: int) -> str:
        payload = self._request("GET", f"/mods/{mod_id}/description")
        return payload.get("data", "")

    def download(self, mod_id: int, file_id: int, destination: Path) -> Path:
        mod = self.get_mod(mod_id)
        if not mod.third_party_distribution_allowed:
            raise CurseForgeError(
                f"Mod {mod_id} does not allow third-party distribution — "
                "fall back to an external link instead of downloading."
            )
        payload = self._request("GET", f"/mods/{mod_id}/files/{file_id}/download-url")
        download_url = payload.get("data")
        if not download_url:
            raise CurseForgeError(f"No download URL available for mod {mod_id} file {file_id}")

        response = self._session.get(
            download_url,
            headers={"x-api-key": self._api_key},
            stream=True,
            timeout=_DOWNLOAD_TIMEOUT,
        )
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as f:
            for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                f.write(chunk)
        return destination
