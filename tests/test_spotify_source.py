"""Unit tests for the Spotify Source adapter (issue #17).

Spotify needs OAuth to read anything, so ``metadata`` is a no-op (the caller
records the id/type it parses) and there is no art.
"""
import app as app_module
from app import SpotifySource


def _no_network(monkeypatch):
    def _get(url, **kwargs):
        raise AssertionError("Spotify adapter must not reach the network")

    monkeypatch.setattr(app_module, "http_get", _get)


# --- url() : id -> URL -----------------------------------------------------

def test_url_known_media_types():
    src = SpotifySource()
    assert src.url("album", "abc123") == "https://open.spotify.com/album/abc123"
    assert src.url("track", "def456") == "https://open.spotify.com/track/def456"


def test_url_empty_id_is_blank():
    assert SpotifySource().url("album", "") == ""


# --- parse_url() : URL -> id (alphanumeric ids) ----------------------------

def test_parse_url_extracts_alphanumeric_id():
    src = SpotifySource()
    assert src.parse_url("https://open.spotify.com/album/6aBxqD") == (
        "album",
        "6aBxqD",
    )


def test_parse_url_no_match_is_none():
    assert SpotifySource().parse_url("https://open.spotify.com/") == (None, None)


# --- album_art() / metadata() : no fetch -----------------------------------

def test_album_art_is_empty(monkeypatch):
    _no_network(monkeypatch)
    assert SpotifySource().album_art("abc", "album") == {"album_art": ""}


def test_metadata_is_noop(monkeypatch):
    _no_network(monkeypatch)
    assert SpotifySource().metadata("https://open.spotify.com/album/abc") == {}
