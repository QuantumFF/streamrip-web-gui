"""Unit tests for the SoundCloud Source adapter (issue #17).

SoundCloud art is always empty and there is no metadata fetch. The adapter owns
the id-mangling that used to live in the album-art endpoint: a ``id|...`` split
and a ``soundcloud:tracks:<n>`` urn extraction.
"""
from app import SoundCloudSource


# --- normalize_id() : the carried-over id-mangling -------------------------

def test_normalize_id_splits_on_pipe():
    assert SoundCloudSource().normalize_id("123456|extra|stuff") == "123456"


def test_normalize_id_extracts_track_urn():
    assert SoundCloudSource().normalize_id("soundcloud:tracks:987654") == "987654"


def test_normalize_id_pipe_takes_precedence_over_urn():
    # A pipe is handled first, matching the original endpoint's if/elif order.
    assert SoundCloudSource().normalize_id("abc|soundcloud:tracks:1") == "abc"


def test_normalize_id_plain_id_unchanged():
    assert SoundCloudSource().normalize_id("plain-id") == "plain-id"


def test_normalize_id_empty_unchanged():
    assert SoundCloudSource().normalize_id("") == ""


# --- url() : id -> URL -----------------------------------------------------

def test_url_uses_path_id():
    assert SoundCloudSource().url("track", "user/song") == (
        "https://soundcloud.com/user/song"
    )


def test_url_empty_id_is_blank():
    assert SoundCloudSource().url("track", "") == ""


# --- album_art() : always empty --------------------------------------------

def test_album_art_is_always_empty():
    assert SoundCloudSource().album_art("123", "track") == {"album_art": ""}
    assert SoundCloudSource().album_art("123", "artist") == {"album_art": ""}
