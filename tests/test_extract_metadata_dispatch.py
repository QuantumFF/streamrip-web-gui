"""extract_metadata_from_url now dispatches over the Source registry by domain
instead of a per-source if/elif chain (issue #17). These cover the no-HTTP
sources whose adapters landed in this slice.
"""
import app as app_module
from app import extract_metadata_from_url


def _no_network(monkeypatch):
    def _get(url, **kwargs):
        raise AssertionError("metadata extraction must not reach the network here")

    monkeypatch.setattr(app_module, "http_get", _get)


def test_tidal_url_records_id_type_and_art(monkeypatch):
    _no_network(monkeypatch)
    meta = extract_metadata_from_url("https://tidal.com/browse/album/111")
    assert meta["service"] == "tidal"
    assert meta["type"] == "album"
    assert meta["id"] == "111"
    assert meta["album_art"] == "https://resources.tidal.com/images/111/320x320.jpg"


def test_spotify_url_records_only_id_and_type(monkeypatch):
    _no_network(monkeypatch)
    meta = extract_metadata_from_url("https://open.spotify.com/track/abc123")
    assert meta["service"] == "spotify"
    assert meta["type"] == "track"
    assert meta["id"] == "abc123"
    # OAuth required -> no title/artist/art fetched.
    assert meta["title"] is None
    assert meta["album_art"] is None


def test_unknown_domain_is_all_none(monkeypatch):
    _no_network(monkeypatch)
    meta = extract_metadata_from_url("https://example.com/whatever")
    assert meta["service"] is None
    assert meta["id"] is None


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_deezer_url_still_dispatches_through_seam(monkeypatch):
    # Guards the registry's domain map: deezer must resolve from its URL and
    # fetch through the http_get seam.
    def _get(url, **kwargs):
        return _FakeResponse(
            {"title": "Discovery", "artist": {"name": "Daft Punk"},
             "cover_medium": "http://art.jpg"}
        )

    monkeypatch.setattr(app_module, "http_get", _get)
    meta = extract_metadata_from_url("https://www.deezer.com/album/111")
    assert meta["service"] == "deezer"
    assert meta["type"] == "album"
    assert meta["id"] == "111"
    assert meta["title"] == "Discovery"
