"""Unit tests for the Qobuz Source adapter (issue #15).

The adapter owns Qobuz URL construction (both directions), album-art, metadata,
and credential/app_id reading. Network access flows through the injectable
``http_get`` seam, which these tests swap for a fake returning canned JSON so the
adapter is exercised with no real network. Credential reading is driven by a
fake streamrip config on disk via the STREAMRIP_CONFIG seam.
"""
import app as app_module
from app import QobuzSource


class FakeResponse:
    """Mimics the slice of requests.Response the adapter uses."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def fake_http_get(payload, status_code=200):
    """Build a fake http_get that records its calls and returns canned JSON."""
    calls = []

    def _get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse(payload, status_code)

    _get.calls = calls
    return _get


# --- url() : id -> URL (one direction) -------------------------------------

def test_url_known_media_types():
    src = QobuzSource()
    assert src.url("album", "111") == "https://open.qobuz.com/album/111"
    assert src.url("track", "222") == "https://open.qobuz.com/track/222"
    assert src.url("artist", "333") == "https://open.qobuz.com/artist/333"
    assert src.url("playlist", "444") == "https://open.qobuz.com/playlist/444"


def test_url_empty_id_is_blank():
    assert QobuzSource().url("album", "") == ""


def test_url_unknown_media_type_falls_back():
    assert QobuzSource().url("label", "9") == "https://open.qobuz.com/label/9"


# --- parse_url() : URL -> id (other direction) -----------------------------

def test_parse_url_extracts_type_and_id():
    src = QobuzSource()
    assert src.parse_url("https://open.qobuz.com/album/111") == ("album", "111")
    assert src.parse_url("https://www.qobuz.com/track/222") == ("track", "222")


def test_parse_url_no_match_is_none():
    assert QobuzSource().parse_url("https://qobuz.com/") == (None, None)


# --- credentials() / app_id() : config reading (single regex) --------------

def _write_config(monkeypatch, tmp_path, content):
    cfg = tmp_path / "config.toml"
    cfg.write_text(content)
    monkeypatch.setattr(app_module, "STREAMRIP_CONFIG", str(cfg))


def test_credentials_read_from_config(monkeypatch, tmp_path):
    _write_config(
        monkeypatch,
        tmp_path,
        'app_id = "123456"\npassword_or_token = "secret-token"\n',
    )
    creds = QobuzSource().credentials()
    assert creds == {"app_id": "123456", "token": "secret-token"}


def test_app_id_uses_same_read(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, 'app_id = "987654"\n')
    src = QobuzSource()
    assert src.app_id() == "987654"
    # No token in config -> credentials still resolve, token None.
    assert src.credentials()["token"] is None


def test_credentials_fall_back_when_config_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        app_module, "STREAMRIP_CONFIG", str(tmp_path / "does-not-exist.toml")
    )
    creds = QobuzSource().credentials()
    assert creds["app_id"] == "950096963"
    assert creds["token"] is None


# --- album_art() through the fake seam -------------------------------------

def test_album_art_fetches_through_http_get(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, 'app_id = "id1"\npassword_or_token = "tok"\n')
    fake = fake_http_get(
        {
            "image": {"large": "http://art/large.jpg", "small": "http://art/small.jpg"},
            "tracks_count": 12,
            "release_type": "album",
            "release_date_original": "2001-06-05",
        }
    )
    monkeypatch.setattr(app_module, "http_get", fake)

    result = QobuzSource().album_art("111", "album")
    assert result["album_art"] == "http://art/large.jpg"
    assert result["tracks_count"] == 12
    assert result["release_type"] == "album"
    assert result["year"] == "2001"
    # The seam was hit with the album endpoint and id, no real network.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://www.qobuz.com/api.json/0.2/album/get"
    assert call["params"]["album_id"] == "111"
    assert call["headers"]["X-User-Auth-Token"] == "tok"


def test_album_art_without_token_returns_empty(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, 'app_id = "id1"\n')  # no token
    fake = fake_http_get({"image": {"large": "x"}})
    monkeypatch.setattr(app_module, "http_get", fake)

    assert QobuzSource().album_art("111", "album") == {}
    # No token -> never reaches the network.
    assert fake.calls == []


def test_album_art_non_200_is_empty(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, 'app_id = "id1"\npassword_or_token = "tok"\n')
    monkeypatch.setattr(app_module, "http_get", fake_http_get({}, status_code=401))
    assert QobuzSource().album_art("111", "album") == {}


# --- metadata() through the fake seam --------------------------------------

def test_album_metadata_through_http_get(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, 'app_id = "id1"\n')
    fake = fake_http_get(
        {
            "title": "OK Computer",
            "artist": {"name": "Radiohead"},
            "image": {"medium": "http://art/medium.jpg"},
        }
    )
    monkeypatch.setattr(app_module, "http_get", fake)

    meta = QobuzSource().metadata("https://open.qobuz.com/album/111")
    assert meta["title"] == "OK Computer"
    assert meta["artist"] == "Radiohead"
    assert meta["album_art"] == "http://art/medium.jpg"
    assert fake.calls[0]["params"]["album_id"] == "111"


def test_track_metadata_through_http_get(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, 'app_id = "id1"\n')
    fake = fake_http_get(
        {
            "title": "Paranoid Android",
            "performer": {"name": "Radiohead"},
            "album": {"image": {"small": "http://art/small.jpg"}},
        }
    )
    monkeypatch.setattr(app_module, "http_get", fake)

    meta = QobuzSource().metadata("https://open.qobuz.com/track/222")
    assert meta["title"] == "Paranoid Android"
    assert meta["artist"] == "Radiohead"
    assert meta["album_art"] == "http://art/small.jpg"
    assert fake.calls[0]["url"] == "https://www.qobuz.com/api.json/0.2/track/get"
    assert fake.calls[0]["params"]["track_id"] == "222"


def test_metadata_unparseable_url_skips_network(monkeypatch):
    fake = fake_http_get({})
    monkeypatch.setattr(app_module, "http_get", fake)
    assert QobuzSource().metadata("https://qobuz.com/no-id-here") == {}
    assert fake.calls == []
