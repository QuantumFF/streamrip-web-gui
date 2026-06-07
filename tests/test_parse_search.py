"""Parsing of `rip search`'s output file into result dicts (pure fn).

parse_search_results is the most correctness-sensitive part of Search: it turns
the raw text `rip search --output-file` wrote into the dicts /api/search serves.
Being pure (no file IO, no subprocess) it is tested directly here, not only
through a live `rip` run.
"""
import json

from app import parse_search_results


def test_wellformed_results_are_parsed():
    content = json.dumps(
        [
            {"id": "111", "source": "qobuz", "media_type": "album", "desc": "OK Computer by Radiohead"},
            {"id": "222", "source": "qobuz", "media_type": "album", "desc": "Kid A by Radiohead"},
        ]
    )
    parsed = parse_search_results(content, "qobuz", "album")

    assert parsed.error is None
    assert len(parsed.results) == 2
    first = parsed.results[0]
    assert first["id"] == "111"
    assert first["service"] == "qobuz"
    assert first["type"] == "album"
    assert first["url"] == "https://open.qobuz.com/album/111"


def test_desc_title_by_artist_split():
    content = json.dumps(
        [{"id": "1", "source": "qobuz", "media_type": "album", "desc": "OK Computer by Radiohead"}]
    )
    item = parse_search_results(content, "qobuz", "album").results[0]
    assert item["title"] == "OK Computer"
    assert item["artist"] == "Radiohead"


def test_desc_without_by_keeps_whole_as_artist():
    content = json.dumps(
        [{"id": "1", "source": "qobuz", "media_type": "artist", "desc": "Radiohead"}]
    )
    item = parse_search_results(content, "qobuz", "artist").results[0]
    # No " by " -> whole desc is the artist, title empty (unchanged behaviour).
    assert item["artist"] == "Radiohead"
    assert item["title"] == ""


def test_rsplit_uses_last_by_separator():
    content = json.dumps(
        [{"id": "1", "source": "qobuz", "media_type": "album", "desc": "Day by Day by Someone"}]
    )
    item = parse_search_results(content, "qobuz", "album").results[0]
    assert item["title"] == "Day by Day"
    assert item["artist"] == "Someone"


def test_missing_optional_fields_fall_back():
    # No id, no source, no media_type, no desc.
    content = json.dumps([{}])
    parsed = parse_search_results(content, "tidal", "track")

    assert parsed.error is None
    item = parsed.results[0]
    assert item["id"] == ""
    # source falls back to the request source, media_type to the search type.
    assert item["service"] == "tidal"
    assert item["type"] == "track"
    assert item["desc"] == ""
    # Empty id -> Source.url returns "".
    assert item["url"] == ""
    assert item["album_art"] == ""


def test_empty_list_parses_to_no_results():
    parsed = parse_search_results("[]", "qobuz", "album")
    assert parsed.error is None
    assert parsed.results == []


def test_empty_content_signals_empty():
    assert parse_search_results("", "qobuz", "album").error == "empty"
    assert parse_search_results("   \n ", "qobuz", "album").error == "empty"
    assert parse_search_results(None, "qobuz", "album").error == "empty"


def test_malformed_json_signals_decode_error_without_raising():
    parsed = parse_search_results("{not json", "qobuz", "album")
    assert parsed.results == []
    assert isinstance(parsed.error, json.JSONDecodeError)
