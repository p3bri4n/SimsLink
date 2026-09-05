from datetime import datetime, timedelta, timezone

import pytest
import requests

from backend import curseforge as cf


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, chunks=None):
        self.status_code = status_code
        self._json = json_data
        self._chunks = chunks or []

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse], *, raise_on: dict[str, Exception] | None = None):
        self._responses = responses
        self._raise_on = raise_on or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method, url, headers=None, timeout=None, params=None, **kwargs):
        self.calls.append((method, url, params))
        key = self._match(url)
        if key in self._raise_on:
            raise self._raise_on[key]
        return self._responses[key]

    def get(self, url, headers=None, stream=None, timeout=None, **kwargs):
        self.calls.append(("GET", url, None))
        key = self._match(url)
        if key in self._raise_on:
            raise self._raise_on[key]
        return self._responses[key]

    def _match(self, url):
        candidates = [key for key in self._responses.keys() | self._raise_on.keys() if key in url]
        if not candidates:
            raise AssertionError(f"No fake response configured for {url}")
        return max(candidates, key=len)  # longest (most specific) match wins


GAMES_PAYLOAD = {"data": [{"id": 78022, "name": "The Sims 4"}, {"id": 432, "name": "Minecraft"}]}


def make_client(responses, **kwargs):
    return cf.CurseForgeClient("test-key", session=FakeSession(responses, **kwargs))


# --- client basics -----------------------------------------------------------


def test_client_requires_api_key():
    with pytest.raises(cf.CurseForgeError):
        cf.CurseForgeClient("")


def test_client_requires_non_blank_api_key():
    with pytest.raises(cf.CurseForgeError):
        cf.CurseForgeClient("   ")


def test_request_raises_auth_error_on_401():
    client = make_client({"/games": FakeResponse(status_code=401)})

    with pytest.raises(cf.CurseForgeAuthError):
        client.game_id()


def test_request_raises_auth_error_on_403():
    client = make_client({"/games": FakeResponse(status_code=403)})

    with pytest.raises(cf.CurseForgeAuthError):
        client.game_id()


# --- verify_key ----------------------------------------------------------------


def test_verify_key_true_on_success():
    client = make_client({"/games": FakeResponse(json_data=GAMES_PAYLOAD)})

    assert client.verify_key() is True


def test_verify_key_false_on_auth_error():
    client = make_client({"/games": FakeResponse(status_code=401)})

    assert client.verify_key() is False


def test_verify_key_false_on_network_error():
    client = make_client({}, raise_on={"/games": requests.ConnectionError("offline")})

    assert client.verify_key() is False


# --- game_id resolution --------------------------------------------------------


def test_game_id_resolves_by_name_and_caches():
    session = FakeSession({"/games": FakeResponse(json_data=GAMES_PAYLOAD)})
    client = cf.CurseForgeClient("test-key", session=session)

    assert client.game_id() == 78022
    assert client.game_id() == 78022
    assert len(session.calls) == 1  # cached after first resolution


def test_game_id_raises_when_sims4_not_in_list():
    client = make_client({"/games": FakeResponse(json_data={"data": [{"id": 432, "name": "Minecraft"}]})})

    with pytest.raises(cf.CurseForgeError):
        client.game_id()


# --- search / get_mod / get_files ----------------------------------------------


def test_search_mods_parses_results():
    client = make_client(
        {
            "/games": FakeResponse(json_data=GAMES_PAYLOAD),
            "/mods/search": FakeResponse(
                json_data={
                    "data": [
                        {
                            "id": 111,
                            "name": "Better Woohoo",
                            "summary": "Makes it better.",
                            "authors": [{"name": "SomeAuthor"}],
                            "categories": [{"name": "Gameplay"}],
                            "logo": {"thumbnailUrl": "https://example.com/thumb.png"},
                            "links": {"websiteUrl": "https://www.curseforge.com/sims4/mods/better-woohoo"},
                            "allowModDistribution": True,
                        }
                    ]
                }
            ),
        }
    )

    results = client.search_mods("woohoo")

    assert len(results) == 1
    mod = results[0]
    assert mod.mod_id == 111
    assert mod.name == "Better Woohoo"
    assert mod.author == "SomeAuthor"
    assert mod.category == "Gameplay"
    assert mod.third_party_distribution_allowed is True


def test_search_mods_treats_missing_distribution_flag_as_allowed():
    client = make_client(
        {
            "/games": FakeResponse(json_data=GAMES_PAYLOAD),
            "/mods/search": FakeResponse(
                json_data={"data": [{"id": 1, "name": "X", "summary": "", "authors": [], "categories": []}]}
            ),
        }
    )

    assert client.search_mods("x")[0].third_party_distribution_allowed is True


def test_search_mods_respects_explicit_distribution_disallowed():
    client = make_client(
        {
            "/games": FakeResponse(json_data=GAMES_PAYLOAD),
            "/mods/search": FakeResponse(
                json_data={
                    "data": [
                        {
                            "id": 1,
                            "name": "X",
                            "summary": "",
                            "authors": [],
                            "categories": [],
                            "allowModDistribution": False,
                        }
                    ]
                }
            ),
        }
    )

    assert client.search_mods("x")[0].third_party_distribution_allowed is False


def test_search_mods_defaults_to_popularity_sort():
    session = FakeSession(
        {"/games": FakeResponse(json_data=GAMES_PAYLOAD), "/mods/search": FakeResponse(json_data={"data": []})}
    )
    client = cf.CurseForgeClient("test-key", session=session)

    client.search_mods("x")

    _, _, params = session.calls[-1]
    assert params["sortField"] == cf.SORT_FIELDS["popularity"]
    assert params["sortOrder"] == "desc"
    assert params["index"] == 0


def test_search_mods_sends_index_for_pagination():
    session = FakeSession(
        {"/games": FakeResponse(json_data=GAMES_PAYLOAD), "/mods/search": FakeResponse(json_data={"data": []})}
    )
    client = cf.CurseForgeClient("test-key", session=session)

    client.search_mods("x", index=50)

    _, _, params = session.calls[-1]
    assert params["index"] == 50


def test_search_mods_sends_sort_field_for_named_sort():
    session = FakeSession(
        {"/games": FakeResponse(json_data=GAMES_PAYLOAD), "/mods/search": FakeResponse(json_data={"data": []})}
    )
    client = cf.CurseForgeClient("test-key", session=session)

    client.search_mods("x", sort="newest")

    _, _, params = session.calls[-1]
    assert params["sortField"] == cf.SORT_FIELDS["newest"]


def test_search_mods_parses_download_count_and_date_modified():
    client = make_client(
        {
            "/games": FakeResponse(json_data=GAMES_PAYLOAD),
            "/mods/search": FakeResponse(
                json_data={
                    "data": [
                        {
                            "id": 1,
                            "name": "X",
                            "summary": "",
                            "authors": [],
                            "categories": [],
                            "downloadCount": 12345,
                            "dateModified": "2026-08-01T00:00:00Z",
                        }
                    ]
                }
            ),
        }
    )

    mod = client.search_mods("x")[0]

    assert mod.download_count == 12345
    assert mod.date_modified == "2026-08-01T00:00:00Z"


def test_search_mods_period_filters_out_mods_outside_the_window():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=400)).isoformat()
    client = make_client(
        {
            "/games": FakeResponse(json_data=GAMES_PAYLOAD),
            "/mods/search": FakeResponse(
                json_data={
                    "data": [
                        {
                            "id": 1,
                            "name": "Recent",
                            "summary": "",
                            "authors": [],
                            "categories": [],
                            "dateModified": recent,
                        },
                        {"id": 2, "name": "Old", "summary": "", "authors": [], "categories": [], "dateModified": old},
                    ]
                }
            ),
        }
    )

    results = client.search_mods("x", period="week")

    assert [m.name for m in results] == ["Recent"]


def test_search_mods_period_excludes_mods_missing_date_modified():
    client = make_client(
        {
            "/games": FakeResponse(json_data=GAMES_PAYLOAD),
            "/mods/search": FakeResponse(
                json_data={"data": [{"id": 1, "name": "NoDate", "summary": "", "authors": [], "categories": []}]}
            ),
        }
    )

    assert client.search_mods("x", period="year") == []


def test_search_mods_without_a_period_ignores_missing_dates():
    client = make_client(
        {
            "/games": FakeResponse(json_data=GAMES_PAYLOAD),
            "/mods/search": FakeResponse(
                json_data={
                    "data": [
                        {"id": 1, "name": "A", "summary": "", "authors": [], "categories": []},
                        {"id": 2, "name": "B", "summary": "", "authors": [], "categories": []},
                    ]
                }
            ),
        }
    )

    assert len(client.search_mods("x")) == 2


def test_get_files_parses_version_range_and_release_type():
    client = make_client(
        {
            "/mods/111/files": FakeResponse(
                json_data={
                    "data": [
                        {
                            "id": 222,
                            "fileName": "better_woohoo.zip",
                            "downloadUrl": "https://example.com/dl/222",
                            "gameVersions": ["1.100", "1.99", "1.101"],
                            "releaseType": 1,
                        }
                    ]
                }
            )
        }
    )

    files = client.get_files(111)

    assert len(files) == 1
    assert files[0].file_id == 222
    assert files[0].release_type == "release"
    assert files[0].game_version_min == "1.99"  # numeric min, not lexicographic
    assert files[0].game_version_max == "1.101"
    assert files[0].dependencies == ()  # absent in the response -> empty, not a crash


def test_regression_game_version_range_is_numeric_not_lexicographic():
    """"1.100" sorts *before* "1.99" as plain strings — game_version_min/max
    must not use that ordering (see _min_max_game_versions()'s docstring for
    the real-world bug this caused)."""
    client = make_client(
        {
            "/mods/111/files": FakeResponse(
                json_data={
                    "data": [
                        {
                            "id": 222,
                            "fileName": "better_woohoo.zip",
                            "downloadUrl": None,
                            "gameVersions": ["1.99", "1.100"],
                            "releaseType": 1,
                        }
                    ]
                }
            )
        }
    )

    files = client.get_files(111)

    assert files[0].game_version_min == "1.99"
    assert files[0].game_version_max == "1.100"


def test_get_files_falls_back_to_lexicographic_on_unparseable_game_version():
    client = make_client(
        {
            "/mods/111/files": FakeResponse(
                json_data={
                    "data": [
                        {
                            "id": 222,
                            "fileName": "better_woohoo.zip",
                            "downloadUrl": None,
                            "gameVersions": ["1.99", "not-a-version"],
                            "releaseType": 1,
                        }
                    ]
                }
            )
        }
    )

    files = client.get_files(111)  # must not raise

    assert files[0].game_version_min == "1.99"
    assert files[0].game_version_max == "not-a-version"


def test_get_files_parses_dependencies():
    client = make_client(
        {
            "/mods/111/files": FakeResponse(
                json_data={
                    "data": [
                        {
                            "id": 222,
                            "fileName": "better_woohoo.zip",
                            "downloadUrl": "https://example.com/dl/222",
                            "gameVersions": [],
                            "releaseType": 1,
                            "dependencies": [
                                {"modId": 55, "relationType": 3},
                                {"modId": 66, "relationType": 2},
                            ],
                        }
                    ]
                }
            )
        }
    )

    deps = client.get_files(111)[0].dependencies

    assert len(deps) == 2
    assert (deps[0].mod_id, deps[0].relation_type) == (55, 3)
    assert (deps[1].mod_id, deps[1].relation_type) == (66, 2)


def test_get_file_single_fetch_parses_response():
    client = make_client(
        {
            "/mods/111/files/222": FakeResponse(
                json_data={
                    "data": {
                        "id": 222,
                        "fileName": "better_woohoo.zip",
                        "downloadUrl": "https://example.com/dl/222",
                        "gameVersions": ["1.100"],
                        "releaseType": 1,
                        "dependencies": [{"modId": 55, "relationType": 3}],
                    }
                }
            )
        }
    )

    file = client.get_file(111, 222)

    assert file.file_id == 222
    assert file.dependencies == (cf.CurseForgeFileDependency(mod_id=55, relation_type=3),)


def test_get_mod_parses_main_file_id():
    client = make_client(
        {
            "/mods/111": FakeResponse(
                json_data={"data": {"id": 111, "name": "Better Woohoo", "mainFileId": 222}}
            )
        }
    )

    assert client.get_mod(111).main_file_id == 222


def test_get_mod_main_file_id_none_when_absent():
    client = make_client({"/mods/111": FakeResponse(json_data={"data": {"id": 111, "name": "X"}})})

    assert client.get_mod(111).main_file_id is None


# --- fingerprint matching -------------------------------------------------------


def test_get_mods_bulk_parses_results():
    client = make_client(
        {
            "/mods": FakeResponse(
                json_data={
                    "data": [
                        {"id": 111, "name": "Better Woohoo", "authors": [{"name": "SomeAuthor"}]},
                        {"id": 222, "name": "Realistic Childbirth", "authors": [{"name": "OtherAuthor"}]},
                    ]
                }
            )
        }
    )

    mods = client.get_mods([111, 222])

    assert [m.mod_id for m in mods] == [111, 222]
    assert mods[0].author == "SomeAuthor"


def test_get_mods_empty_input_makes_no_request():
    client = make_client({})

    assert client.get_mods([]) == []


def test_match_fingerprints_correlates_via_modules_not_array_position():
    # Realistic single-file matches: each exactMatches entry's file.modules
    # is what actually correlates a sent fingerprint back to a modId — see
    # match_fingerprints()'s docstring for why exactFingerprints (a plain
    # echo of the full request) can't be used positionally.
    client = make_client(
        {
            "/fingerprints": FakeResponse(
                json_data={
                    "data": {
                        "exactFingerprints": [111, 222, 333],
                        "exactMatches": [
                            {
                                "id": 91279,
                                "file": {
                                    "id": 4001,
                                    "modId": 91279,
                                    "fileFingerprint": 999999,
                                    "modules": [{"name": "Thing.package", "fingerprint": 111}],
                                },
                            },
                            {
                                "id": 118813,
                                "file": {
                                    "id": 4002,
                                    "modId": 118813,
                                    "fileFingerprint": 888888,
                                    "modules": [{"name": "Other.package", "fingerprint": 222}],
                                },
                            },
                        ],
                    }
                }
            )
        }
    )

    result = client.match_fingerprints([111, 222, 333])

    assert result == {111: (91279, 4001), 222: (118813, 4002)}
    assert 333 not in result  # unmatched, correctly dropped


def test_match_fingerprints_one_file_bundling_two_sent_fingerprints():
    # Regression for the real incident (CLAUDE.md, 2026-08-23): a single
    # CurseForge file (e.g. a .package + .ts4script pair from one archive)
    # can satisfy *two* of our sent fingerprints via its own modules list —
    # exactMatches then has fewer entries than fingerprints sent, which used
    # to desync a naive positional zip() for every pairing that followed.
    client = make_client(
        {
            "/fingerprints": FakeResponse(
                json_data={
                    "data": {
                        "exactFingerprints": [111, 222, 333],
                        "exactMatches": [
                            {
                                "id": 555,
                                "file": {
                                    "id": 5001,
                                    "modId": 555,
                                    "fileFingerprint": 999999,
                                    "modules": [
                                        {"name": "Thing.package", "fingerprint": 111},
                                        {"name": "Thing.ts4script", "fingerprint": 222},
                                    ],
                                },
                            },
                            {
                                "id": 777,
                                "file": {
                                    "id": 5002,
                                    "modId": 777,
                                    "fileFingerprint": 888888,
                                    "modules": [{"name": "Unrelated.package", "fingerprint": 333}],
                                },
                            },
                        ],
                    }
                }
            )
        }
    )

    result = client.match_fingerprints([111, 222, 333])

    # not {333: (555, ...), ...} — the old bug's shift
    assert result == {111: (555, 5001), 222: (555, 5001), 333: (777, 5002)}


def test_match_fingerprints_drops_a_fingerprint_claimed_by_two_different_matches():
    # Should never legitimately happen — dropped rather than guessed at,
    # same "suspicion is not confirmation" rule as everywhere else.
    client = make_client(
        {
            "/fingerprints": FakeResponse(
                json_data={
                    "data": {
                        "exactFingerprints": [111],
                        "exactMatches": [
                            {
                                "id": 1,
                                "file": {"id": 6001, "modId": 1, "modules": [{"name": "A", "fingerprint": 111}]},
                            },
                            {
                                "id": 2,
                                "file": {"id": 6002, "modId": 2, "modules": [{"name": "B", "fingerprint": 111}]},
                            },
                        ],
                    }
                }
            )
        }
    )

    assert client.match_fingerprints([111]) == {}


def test_match_fingerprints_ignores_partial_matches():
    client = make_client(
        {
            "/fingerprints": FakeResponse(
                json_data={
                    "data": {
                        "exactFingerprints": [],
                        "exactMatches": [],
                        "partialMatches": [{"id": 555, "file": {"fileFingerprint": 111}}],
                    }
                }
            )
        }
    )

    assert client.match_fingerprints([111]) == {}


def test_match_fingerprints_empty_input_makes_no_request():
    client = make_client({})

    assert client.match_fingerprints([]) == {}


# --- curseforge_fingerprint (pure function, no network) -------------------------
#
# Values below are regression fixtures, not independently-sourced test
# vectors — computed once with this implementation and then verified
# directly against the real CurseForge API (a real installed .package file's
# fingerprint correctly round-tripped to its real CurseForge mod — see
# CLAUDE.md's fingerprint-matching notes). Protects against a future
# accidental change to the hash breaking real matching silently.


def test_curseforge_fingerprint_known_values():
    assert cf.curseforge_fingerprint(b"") == 1540447798
    assert cf.curseforge_fingerprint(b"hello world") == 2824650221


def test_curseforge_fingerprint_strips_whitespace_before_hashing():
    # CurseForge's own quirk: tab/LF/CR/space bytes are removed before
    # hashing, so these two must hash identically.
    assert cf.curseforge_fingerprint(b"a b\tc\nd\re") == cf.curseforge_fingerprint(b"abcde")


def test_curseforge_fingerprint_handles_non_multiple_of_four_length():
    # Exercises the tail-byte handling (1/2/3 leftover bytes) — a length
    # that's an exact multiple of 4 never reaches that code path at all.
    assert cf.curseforge_fingerprint(b"abcde") is not None
    assert cf.curseforge_fingerprint(b"abcde") != cf.curseforge_fingerprint(b"abcd")


# --- compat_status (pure function, no network) ---------------------------------


def test_compat_status_compatible():
    assert cf.compat_status("1.90", "1.110", "1.100") == "compatible"


def test_compat_status_incompatible_too_old():
    assert cf.compat_status("1.95", None, "1.90") == "incompatible"


def test_compat_status_incompatible_too_new():
    assert cf.compat_status(None, "1.95", "1.100") == "incompatible"


def test_compat_status_unknown_when_no_version_info():
    assert cf.compat_status(None, None, "1.100") == "unknown"


def test_compat_status_unknown_when_current_version_missing():
    assert cf.compat_status("1.90", "1.110", None) == "unknown"


def test_compat_status_unknown_on_unparseable_version():
    assert cf.compat_status("latest", None, "1.100") == "unknown"


# --- download --------------------------------------------------------------------


def test_download_raises_when_distribution_not_allowed(tmp_path):
    client = make_client(
        {
            "/mods/111": FakeResponse(
                json_data={
                    "data": {
                        "id": 111,
                        "name": "X",
                        "summary": "",
                        "authors": [],
                        "categories": [],
                        "allowModDistribution": False,
                    }
                }
            )
        }
    )

    with pytest.raises(cf.CurseForgeError):
        client.download(111, 222, tmp_path / "out.zip")


def test_download_raises_when_no_download_url(tmp_path):
    client = make_client(
        {
            "/mods/111": FakeResponse(
                json_data={"data": {"id": 111, "name": "X", "summary": "", "authors": [], "categories": []}}
            ),
            "/mods/111/files/222/download-url": FakeResponse(json_data={"data": None}),
        }
    )

    with pytest.raises(cf.CurseForgeError):
        client.download(111, 222, tmp_path / "out.zip")


def test_download_writes_file_content(tmp_path):
    client = make_client(
        {
            "/mods/111": FakeResponse(
                json_data={"data": {"id": 111, "name": "X", "summary": "", "authors": [], "categories": []}}
            ),
            "/mods/111/files/222/download-url": FakeResponse(
                json_data={"data": "https://cdn.example.com/better_woohoo.zip"}
            ),
            "cdn.example.com": FakeResponse(chunks=[b"hello ", b"world"]),
        }
    )

    destination = tmp_path / "downloaded" / "out.zip"
    result = client.download(111, 222, destination)

    assert result == destination
    assert destination.read_bytes() == b"hello world"
