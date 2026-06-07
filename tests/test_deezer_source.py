"""Unit tests for the Deezer Source adapter (issue #16).

The adapter owns Deezer URL construction (both directions), album-art, and
metadata. Deezer needs no credentials. Its art is asymmetric: album/track art
is a pure URL template (no HTTP), while artist art is a real API call through
the injectable ``http_get`` seam, which these tests swap for a fake returning
canned JSON so the adapter is exercised with no real network.
"""
import app as app_module
from app import DeezerSource


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
    src = DeezerSource()
    assert src.url("album", "111") == "https://www.deezer.com/album/111"
    assert src.url("track", "222") == "https://www.deezer.com/track/222"
    assert src.url("artist", "333") == "https://www.deezer.com/artist/333"
    assert src.url("playlist", "444") == "https://www.deezer.com/playlist/444"


def test_url_empty_id_is_blank():
    assert DeezerSource().url("album", "") == ""


def test_url_unknown_media_type_falls_back():
    assert DeezerSource().url("label", "9") == "https://www.deezer.com/label/9"


# --- parse_url() : URL -> id (other direction) -----------------------------

def test_parse_url_extracts_type_and_id():
    src = DeezerSource()
    assert src.parse_url("https://www.deezer.com/album/111") == ("album", "111")
    assert src.parse_url("https://deezer.com/track/222") == ("track", "222")


def test_parse_url_no_match_is_none():
    assert DeezerSource().parse_url("https://www.deezer.com/") == (None, None)


# --- album_art() : album/track use template (no HTTP) ----------------------

def test_album_art_album_is_template_no_network(monkeypatch):
    fake = fake_http_get({})
    monkeypatch.setattr(app_module, "http_get", fake)
    result = DeezerSource().album_art("111", "album")
    assert result == {"album_art": "https://api.deezer.com/album/111/image"}
    # Template art never reaches the network.
    assert fake.calls == []


def test_album_art_track_is_template_no_network(monkeypatch):
    fake = fake_http_get({})
    monkeypatch.setattr(app_module, "http_get", fake)
    result = DeezerSource().album_art("222", "track")
    assert result == {"album_art": "https://api.deezer.com/track/222/image"}
    assert fake.calls == []


# --- album_art() : artist uses the API via http_get ------------------------

def test_album_art_artist_uses_api(monkeypatch):
    fake = fake_http_get(
        {"picture_medium": "http://art/medium.jpg", "picture": "http://art/x.jpg"}
    )
    monkeypatch.setattr(app_module, "http_get", fake)
    result = DeezerSource().album_art("333", "artist")
    assert result == {"album_art": "http://art/medium.jpg"}
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == "https://api.deezer.com/artist/333"


def test_album_art_artist_falls_back_to_picture(monkeypatch):
    fake = fake_http_get({"picture": "http://art/x.jpg"})
    monkeypatch.setattr(app_module, "http_get", fake)
    assert DeezerSource().album_art("333", "artist") == {
        "album_art": "http://art/x.jpg"
    }


def test_album_art_artist_non_200_is_empty(monkeypatch):
    monkeypatch.setattr(app_module, "http_get", fake_http_get({}, status_code=404))
    assert DeezerSource().album_art("333", "artist") == {}


# --- metadata() through the fake seam --------------------------------------

def test_album_metadata_through_http_get(monkeypatch):
    fake = fake_http_get(
        {
            "title": "Discovery",
            "artist": {"name": "Daft Punk"},
            "cover_medium": "http://art/medium.jpg",
        }
    )
    monkeypatch.setattr(app_module, "http_get", fake)

    meta = DeezerSource().metadata("https://www.deezer.com/album/111")
    assert meta["title"] == "Discovery"
    assert meta["artist"] == "Daft Punk"
    assert meta["album_art"] == "http://art/medium.jpg"
    assert fake.calls[0]["url"] == "https://api.deezer.com/album/111"


def test_track_metadata_through_http_get(monkeypatch):
    fake = fake_http_get(
        {
            "title": "One More Time",
            "artist": {"name": "Daft Punk"},
            "album": {"cover_medium": "http://art/small.jpg"},
        }
    )
    monkeypatch.setattr(app_module, "http_get", fake)

    meta = DeezerSource().metadata("https://www.deezer.com/track/222")
    assert meta["title"] == "One More Time"
    assert meta["artist"] == "Daft Punk"
    assert meta["album_art"] == "http://art/small.jpg"
    assert fake.calls[0]["url"] == "https://api.deezer.com/track/222"


def test_metadata_unparseable_url_skips_network(monkeypatch):
    fake = fake_http_get({})
    monkeypatch.setattr(app_module, "http_get", fake)
    assert DeezerSource().metadata("https://www.deezer.com/no-id-here") == {}
    assert fake.calls == []
