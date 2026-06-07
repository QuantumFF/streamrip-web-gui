"""Unit tests for the Tidal Source adapter (issue #17).

Tidal is template-only: ``url`` and ``album_art`` are pure URL templates with no
HTTP, and ``metadata`` yields the art URL plus the id/type read out of the URL.
"""
import app as app_module
from app import TidalSource


def _no_network(monkeypatch):
    """Swap http_get for a recorder that fails the test if any source touches it."""
    calls = []

    def _get(url, **kwargs):
        calls.append(url)
        raise AssertionError("Tidal adapter must not reach the network")

    monkeypatch.setattr(app_module, "http_get", _get)
    return calls


# --- url() : id -> URL -----------------------------------------------------

def test_url_known_media_types():
    src = TidalSource()
    assert src.url("album", "111") == "https://tidal.com/browse/album/111"
    assert src.url("track", "222") == "https://tidal.com/browse/track/222"
    assert src.url("artist", "333") == "https://tidal.com/browse/artist/333"
    assert src.url("playlist", "444") == "https://tidal.com/browse/playlist/444"


def test_url_empty_id_is_blank():
    assert TidalSource().url("album", "") == ""


def test_url_unknown_media_type_falls_back():
    assert TidalSource().url("mix", "9") == "https://tidal.com/browse/mix/9"


# --- parse_url() : URL -> id -----------------------------------------------

def test_parse_url_extracts_type_and_id():
    src = TidalSource()
    assert src.parse_url("https://tidal.com/browse/album/111") == ("album", "111")
    assert src.parse_url("https://tidal.com/track/222") == ("track", "222")


def test_parse_url_no_match_is_none():
    assert TidalSource().parse_url("https://tidal.com/") == (None, None)


# --- album_art() : pure template, no HTTP ----------------------------------

def test_album_art_non_artist_is_320_template(monkeypatch):
    _no_network(monkeypatch)
    assert TidalSource().album_art("111", "album") == {
        "album_art": "https://resources.tidal.com/images/111/320x320.jpg"
    }


def test_album_art_artist_is_750_template(monkeypatch):
    _no_network(monkeypatch)
    assert TidalSource().album_art("333", "artist") == {
        "album_art": "https://resources.tidal.com/images/333/750x750.jpg"
    }


# --- metadata() : art URL + parsed id, no HTTP -----------------------------

def test_metadata_yields_art_from_parsed_id(monkeypatch):
    _no_network(monkeypatch)
    meta = TidalSource().metadata("https://tidal.com/browse/album/111")
    assert meta == {
        "album_art": "https://resources.tidal.com/images/111/320x320.jpg"
    }


def test_metadata_unparseable_url_is_empty(monkeypatch):
    _no_network(monkeypatch)
    assert TidalSource().metadata("https://tidal.com/no-id-here") == {}
