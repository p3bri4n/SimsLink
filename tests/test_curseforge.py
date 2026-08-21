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
    assert files[0].game_version_min == "1.100"  # lexicographic min of the raw strings
    assert files[0].game_version_max == "1.99"


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
