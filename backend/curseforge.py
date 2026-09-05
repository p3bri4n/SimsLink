"""CurseForge API client (Direct Mode only).

The only module allowed to require CURSEFORGE_API_KEY. Nothing on the
Assisted Mode code path imports or calls this module (see download_watcher.py
and brief section 4bis).

A real API key has since been added and verified (see CLAUDE.md's "Current
project status", 2026-08-23) — search_mods()/get_mod()/get_files() and the
fingerprint-matching pair (curseforge_fingerprint()/match_fingerprints()/
get_mods()) have all been exercised against the real API at least once, so
the response shapes _parse_mod()/_parse_file() rely on are confirmed
correct for this game, not just guessed from the published REST structure.
_parse_mod()/_parse_file() stay isolated regardless, in case a field ever
does need adjusting later. The game's numeric CurseForge id is likewise
never hardcoded (a guessed id that's wrong would silently return empty
results) — game_id() resolves it once by name via /games and caches it.

Never make an automated request to any CurseForge endpoint without a valid
API key — this includes CDN file downloads, which have required a key since
July 16, 2026 (see CLAUDE.md's "Things to never do").
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

DEFAULT_BASE_URL = "https://api.curseforge.com/v1"
_SIMS4_GAME_NAME = "The Sims 4"
_REQUEST_TIMEOUT = 15
_DOWNLOAD_TIMEOUT = 30
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
FINGERPRINT_BATCH_SIZE = 500  # public: callers batch match_fingerprints()/get_mods() themselves
_SEARCH_PAGE_SIZE = 50  # CurseForge's own max for /mods/search — confirmed live, 100+ is a 400

# CurseForge's /mods/search sortField is an undocumented numeric enum (the
# public docs list the valid 1-12 range but not what each number means).
# Confirmed empirically against the real API (see CLAUDE.md): sorting desc
# by field 6 produces a strictly-decreasing downloadCount (TotalDownloads),
# field 3 a strictly-decreasing dateModified (LastUpdated), field 11 a
# strictly-decreasing dateReleased with brand-new/zero-download mods
# (ReleaseDate), field 4 groups by name (Name), and field 2 gives CurseForge's
# own curated "Popularity" ranking (not a pure downloadCount sort — it does
# not correlate with either date or download count alone).
SORT_FIELDS = {
    "popularity": 2,
    "downloads": 6,
    "updated": 3,
    "newest": 11,
    "name": 4,
}

# CurseForge's search API has no server-side date-range filter, so a "period"
# choice is applied client-side (here) against each result's own dateModified
# — only within whatever single page was already fetched (see search_mods),
# not by paging further to backfill a full page of matches. That's a
# deliberate simplicity tradeoff: a period narrow enough to filter out most of
# one sort's page can legitimately return few or no results, same as a search
# query that just doesn't match much — not a bug to work around with extra
# pagination.
PERIOD_WINDOWS = {
    "week": timedelta(days=7),
    "month": timedelta(days=30),
    "quarter": timedelta(days=90),
    "year": timedelta(days=365),
}

_RELEASE_TYPES = {1: "release", 2: "beta", 3: "alpha"}

_FP_MASK32 = 0xFFFFFFFF
_FP_WHITESPACE_BYTES = (0x09, 0x0A, 0x0D, 0x20)  # tab, LF, CR, space


def curseforge_fingerprint(data: bytes) -> int:
    """CurseForge's own file-identification scheme — a 32-bit MurmurHash2
    (seed 1) computed after stripping whitespace bytes from the file. This
    is the exact mechanism the official CurseForge app uses to recognize a
    file dropped in by hand ("fingerprint matching"); it's platform-wide,
    not specific to any one game, and verified directly against the real
    API for Sims 4 content (see CLAUDE.md's fingerprint-matching notes) —
    every exact match checked so far correctly identified both the right
    mod and the right file variant (e.g. the right translation).
    """
    filtered = bytes(b for b in data if b not in _FP_WHITESPACE_BYTES)
    return _murmur2_32(filtered, 1)


def _murmur2_32(data: bytes, seed: int) -> int:
    # struct.unpack_from does the 4-byte-word extraction in C — the mixing
    # loop itself is still one Python-level iteration per word, so this is
    # still too slow for library-scale batches over very large files (a
    # few hundred MB): see curseforge_match.py's own size cap for why that
    # case is deliberately skipped rather than optimized further here.
    m = 0x5BD1E995
    r = 24
    length = len(data)
    h = (seed ^ length) & _FP_MASK32
    nwords = length // 4
    if nwords:
        for k in struct.unpack_from(f"<{nwords}I", data, 0):
            k = (k * m) & _FP_MASK32
            k ^= k >> r
            k = (k * m) & _FP_MASK32
            h = (h * m) & _FP_MASK32
            h ^= k
    tail_start = nwords * 4
    tail_len = length - tail_start
    if tail_len == 3:
        h ^= data[tail_start + 2] << 16
    if tail_len >= 2:
        h ^= data[tail_start + 1] << 8
    if tail_len >= 1:
        h ^= data[tail_start]
        h = (h * m) & _FP_MASK32
    h ^= h >> 13
    h = (h * m) & _FP_MASK32
    h ^= h >> 15
    return h & _FP_MASK32


class CurseForgeError(Exception):
    pass


class CurseForgeAuthError(CurseForgeError):
    """The configured key was rejected (missing/invalid/expired)."""


@dataclass(frozen=True)
class CurseForgeFileDependency:
    mod_id: int
    relation_type: int  # raw CurseForge FileRelationType enum — interpreted in curseforge_dependencies.py


@dataclass(frozen=True)
class CurseForgeFile:
    file_id: int
    file_name: str
    download_url: str | None
    game_version_min: str | None
    game_version_max: str | None
    release_type: str
    dependencies: tuple[CurseForgeFileDependency, ...] = ()


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
    main_file_id: int | None = None
    download_count: int = 0
    date_modified: str | None = None  # raw ISO 8601 string, as returned by the API


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


def _parse_iso(value: str) -> datetime:
    # CurseForge timestamps end in "Z" (UTC) — fromisoformat() has accepted
    # that directly since Python 3.11, which this project already requires.
    return datetime.fromisoformat(value)


def _min_max_game_versions(game_versions: list[str]) -> tuple[str | None, str | None]:
    """Regression, found 2026-08-24: a file's `gameVersions` (e.g.
    ["1.99", "1.100", "1.101"]) used to be reduced via plain min()/max() over
    the raw strings — lexicographic, not numeric, so "1.100" sorted *below*
    "1.99" the moment the game crossed .99 into triple digits, silently
    producing a wrong game_version_min/max and a wrong compat_status badge
    even though compat_status() itself does a real numeric comparison once
    it receives these values. Now sorts by _version_tuple() (the same
    numeric parser compat_status() already trusts) whenever every entry
    parses cleanly; a single unparseable entry (unexpected — CurseForge's
    gameVersions for this game has always been plain dotted numbers so far,
    but nothing guarantees that forever) falls back to the old lexicographic
    behavior for the whole list rather than crashing on a genuinely
    malformed response."""
    if not game_versions:
        return None, None
    try:
        return (
            min(game_versions, key=_version_tuple),
            max(game_versions, key=_version_tuple),
        )
    except ValueError:
        return min(game_versions), max(game_versions)


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
        main_file_id=data.get("mainFileId"),
        download_count=data.get("downloadCount", 0),
        date_modified=data.get("dateModified"),
    )


def _parse_file(data: dict) -> CurseForgeFile:
    game_versions = data.get("gameVersions") or []
    game_version_min, game_version_max = _min_max_game_versions(game_versions)
    return CurseForgeFile(
        file_id=data["id"],
        file_name=data.get("fileName", ""),
        download_url=data.get("downloadUrl"),
        game_version_min=game_version_min,
        game_version_max=game_version_max,
        release_type=_RELEASE_TYPES.get(data.get("releaseType"), "unknown"),
        dependencies=tuple(
            CurseForgeFileDependency(mod_id=dep["modId"], relation_type=dep["relationType"])
            for dep in data.get("dependencies") or []
        ),
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
        self,
        query: str,
        *,
        sort: str = "popularity",
        period: str | None = None,
        page_size: int = _SEARCH_PAGE_SIZE,
    ) -> list[CurseForgeMod]:
        params = {
            "gameId": self.game_id(),
            "searchFilter": query,
            "pageSize": page_size,
            "sortField": SORT_FIELDS.get(sort, SORT_FIELDS["popularity"]),
            "sortOrder": "desc",
        }
        payload = self._request("GET", "/mods/search", params=params)
        mods = [_parse_mod(item) for item in payload.get("data", [])]
        window = PERIOD_WINDOWS.get(period) if period else None
        if window:
            cutoff = datetime.now(timezone.utc) - window
            mods = [m for m in mods if m.date_modified and _parse_iso(m.date_modified) >= cutoff]
        return mods

    def get_mod(self, mod_id: int) -> CurseForgeMod:
        payload = self._request("GET", f"/mods/{mod_id}")
        return _parse_mod(payload["data"])

    def get_mods(self, mod_ids: list[int]) -> list[CurseForgeMod]:
        """Bulk counterpart to get_mod() — one request for many ids, used
        after match_fingerprints() to fetch full metadata (author,
        thumbnail, ...) for every distinct mod a batch of fingerprints
        resolved to, instead of one request per match."""
        if not mod_ids:
            return []
        payload = self._request("POST", "/mods", json={"modIds": mod_ids})
        return [_parse_mod(item) for item in payload.get("data", [])]

    def match_fingerprints(self, fingerprints: list[int]) -> dict[int, int]:
        """Exact fingerprint matches only -> {fingerprint: curseforge_mod_id}.
        CurseForge's own partial-match results are deliberately ignored —
        a similarity guess, not a confirmed identity, same "suspicion is
        not confirmation" rule this app applies everywhere else. Caller is
        responsible for batching (see _FINGERPRINT_BATCH_SIZE) — this sends
        exactly what it's given in one request.

        Regression, found 2026-08-23 via a real-library data-integrity
        incident (see CLAUDE.md): a prior version of this method paired the
        response's top-level exactFingerprints with exactMatches
        positionally (`zip(exactFingerprints, exactMatches)`), on the
        assumption that both arrays line up 1:1, in request order. Verified
        directly against the real API that this assumption is *wrong*:
        exactFingerprints is simply an echo of every fingerprint that was
        sent (same length and order as the request, matched or not), while
        exactMatches only ever contains one entry per matched *file* —
        and a single CurseForge file can bundle more than one of the
        fingerprints we sent (e.g. its .package and its .ts4script,
        submitted as two separate locally-loose mods, both listed under
        that one file's `modules`). Whenever that happens, exactMatches is
        shorter than exactFingerprints and every pairing after that point
        silently shifts — which is exactly what corrupted curseforge_id for
        a real, reproducible set of unrelated mods before this fix.

        Now correlates by *content* instead of position: each exactMatches
        entry's own file.modules[].fingerprint (or, for a match with no
        modules, its file.fileFingerprint) is checked against what was
        actually sent, so the mapping holds regardless of array length,
        order, or how many of our fingerprints one matched file accounts
        for. A fingerprint claimed by two different matches (contradictory,
        should never legitimately happen) is dropped rather than guessed at.
        """
        if not fingerprints:
            return {}
        requested = set(fingerprints)
        payload = self._request("POST", "/fingerprints", json={"fingerprints": fingerprints})
        data = payload.get("data", {})
        exact_matches = data.get("exactMatches") or []

        result: dict[int, int] = {}
        ambiguous: set[int] = set()
        for match in exact_matches:
            file_data = match["file"]
            mod_id = file_data["modId"]
            candidate_fingerprints = [module.get("fingerprint") for module in file_data.get("modules") or []]
            if not candidate_fingerprints:
                candidate_fingerprints = [file_data.get("fileFingerprint")]
            for fp in candidate_fingerprints:
                if fp not in requested:
                    continue
                if fp in result and result[fp] != mod_id:
                    ambiguous.add(fp)
                    continue
                result[fp] = mod_id
        for fp in ambiguous:
            result.pop(fp, None)
        return result

    def get_files(self, mod_id: int) -> list[CurseForgeFile]:
        payload = self._request("GET", f"/mods/{mod_id}/files")
        return [_parse_file(item) for item in payload.get("data", [])]

    def get_file(self, mod_id: int, file_id: int) -> CurseForgeFile:
        """Single-file counterpart to get_files() — used by
        curseforge_dependencies.py to fetch just the mod's main file's own
        declared dependencies, without pulling its whole version history."""
        payload = self._request("GET", f"/mods/{mod_id}/files/{file_id}")
        return _parse_file(payload["data"])

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
