"""Seam 1: the Library tree endpoints, driven through the Flask test client
against a temp download directory.

The Library is the set of already-downloaded albums as they exist on disk
(CONTEXT.md / Library glossary). These tests stub the tag-reading seam
(read_audio_tags) so they exercise the folder-listing and per-album endpoints
without mutagen and without touching real audio files. They never shell out to
real `rip`.
"""
import os

import pytest

import app as app_module


def _make_album(base, artist, album, audio_files, extra_files=()):
    """Create an Artist/Album folder containing the given audio files (and any
    non-audio files, e.g. cover art) under the temp download dir."""
    album_dir = os.path.join(base, artist, album)
    os.makedirs(album_dir, exist_ok=True)
    for name in audio_files:
        open(os.path.join(album_dir, name), 'wb').close()
    for name in extra_files:
        open(os.path.join(album_dir, name), 'wb').close()
    return album_dir


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Point the app at a temp download dir and return its path. Restores the
    real DOWNLOAD_DIR and tag reader afterwards via monkeypatch."""
    base = str(tmp_path / 'Music')
    os.makedirs(base, exist_ok=True)
    monkeypatch.setattr(app_module, 'DOWNLOAD_DIR', base)
    return base


def _albums(client):
    return client.get('/api/library').get_json()['albums']


def _tracks(client, path):
    resp = client.get('/api/library/album', query_string={'path': path})
    return resp


def test_library_lists_album_folders_grouped_by_artist(client, library):
    _make_album(library, 'Radiohead', 'OK Computer', ['01.flac', '02.flac'])
    _make_album(library, 'Boards of Canada', 'Geogaddi', ['1.mp3'])

    albums = _albums(client)
    pairs = {(a['artist'], a['album']) for a in albums}
    assert ('Radiohead', 'OK Computer') in pairs
    assert ('Boards of Canada', 'Geogaddi') in pairs
    # Sorted by artist then album.
    assert albums[0]['artist'] == 'Boards of Canada'


def test_library_listing_reads_no_tags(client, library, monkeypatch):
    # Listing album folders must be a cheap walk: it must never call the tag
    # reader (which is what keeps it instant on large libraries).
    _make_album(library, 'Artist', 'Album', ['01.flac', '02.flac'])

    def boom(filepath):
        raise AssertionError("listing albums must not read tags")

    monkeypatch.setattr(app_module, 'read_audio_tags', boom)

    albums = _albums(client)
    assert len(albums) == 1


def test_library_empty_when_no_albums(client, library):
    assert _albums(client) == []


def test_folder_without_audio_is_not_an_album(client, library):
    # An artist folder that only holds non-audio files is not an album folder.
    bare = os.path.join(library, 'SomeArtist')
    os.makedirs(bare, exist_ok=True)
    open(os.path.join(bare, 'notes.txt'), 'wb').close()
    assert _albums(client) == []


def test_album_tracks_use_tagged_title_and_number(client, library, monkeypatch):
    rel = os.path.relpath(
        _make_album(library, 'Artist', 'Album', ['a.flac', 'b.flac']),
        library,
    )

    tags = {
        'a.flac': {'title': 'Opener', 'tracknumber': '1', 'tracktotal': '2'},
        'b.flac': {'title': 'Closer', 'tracknumber': '2', 'tracktotal': '2'},
    }
    monkeypatch.setattr(
        app_module, 'read_audio_tags',
        lambda fp: tags[os.path.basename(fp)],
    )

    resp = _tracks(client, rel)
    assert resp.status_code == 200
    tracks = resp.get_json()['tracks']
    assert [t['title'] for t in tracks] == ['Opener', 'Closer']
    assert [t['tracknumber'] for t in tracks] == [1, 2]


def test_album_tracks_sorted_by_track_number(client, library, monkeypatch):
    # Files on disk in arbitrary order must come back sorted by track number.
    rel = os.path.relpath(
        _make_album(library, 'Artist', 'Album', ['z.flac', 'a.flac', 'm.flac']),
        library,
    )
    tags = {
        'z.flac': {'title': 'Three', 'tracknumber': '3'},
        'a.flac': {'title': 'One', 'tracknumber': '1'},
        'm.flac': {'title': 'Two', 'tracknumber': '2'},
    }
    monkeypatch.setattr(
        app_module, 'read_audio_tags',
        lambda fp: tags[os.path.basename(fp)],
    )

    tracks = _tracks(client, rel).get_json()['tracks']
    assert [t['title'] for t in tracks] == ['One', 'Two', 'Three']


def test_non_audio_files_are_not_listed_as_tracks(client, library, monkeypatch):
    # Cover art and other non-audio files share the album folder but must never
    # appear as tracks.
    rel = os.path.relpath(
        _make_album(
            library, 'Artist', 'Album',
            audio_files=['01.flac'],
            extra_files=['cover.jpg', 'folder.png', 'streamrip.log'],
        ),
        library,
    )
    monkeypatch.setattr(
        app_module, 'read_audio_tags',
        lambda fp: {'title': 'Only Track', 'tracknumber': '1'},
    )

    tracks = _tracks(client, rel).get_json()['tracks']
    assert len(tracks) == 1
    assert tracks[0]['title'] == 'Only Track'
    filenames = [t['filename'] for t in tracks]
    assert 'cover.jpg' not in filenames


def test_track_falls_back_to_filename_when_untagged(client, library, monkeypatch):
    # A present file whose tags are unreadable is still shown (present tracks are
    # never hidden); its title falls back to the filename.
    rel = os.path.relpath(
        _make_album(library, 'Artist', 'Album', ['mystery.flac']),
        library,
    )
    monkeypatch.setattr(app_module, 'read_audio_tags', lambda fp: {})

    tracks = _tracks(client, rel).get_json()['tracks']
    assert len(tracks) == 1
    assert tracks[0]['title'] == 'mystery.flac'
    assert tracks[0]['tracknumber'] is None


def test_album_tracks_requires_path(client, library):
    resp = client.get('/api/library/album')
    assert resp.status_code == 400


def test_album_tracks_rejects_path_traversal(client, library):
    resp = _tracks(client, '../../etc')
    assert resp.status_code in (400, 404)


def test_album_tracks_unknown_path_is_404(client, library):
    resp = _tracks(client, 'No/Such/Album')
    assert resp.status_code == 404


def test_album_endpoint_reports_complete(client, library, monkeypatch):
    rel = os.path.relpath(
        _make_album(library, 'Artist', 'Album', ['a.flac', 'b.flac']),
        library,
    )
    tags = {
        'a.flac': {'title': 'One', 'tracknumber': '1', 'tracktotal': '2'},
        'b.flac': {'title': 'Two', 'tracknumber': '2', 'tracktotal': '2'},
    }
    monkeypatch.setattr(app_module, 'read_audio_tags',
                        lambda fp: tags[os.path.basename(fp)])

    data = _tracks(client, rel).get_json()
    assert data['completeness']['status'] == 'complete'
    assert data['completeness']['missing_count'] == 0
    # No gap rows: every row is a present track.
    assert all(t['missing'] is False for t in data['tracks'])


def test_album_endpoint_reports_incomplete_with_gap_rows(client, library, monkeypatch):
    # Track 2 of 3 missing on disk -> INCOMPLETE (1 missing), with a greyed gap
    # row at sequence position 2.
    rel = os.path.relpath(
        _make_album(library, 'Artist', 'Album', ['t1.flac', 't3.flac']),
        library,
    )
    tags = {
        't1.flac': {'title': 'One', 'tracknumber': '1', 'tracktotal': '3'},
        't3.flac': {'title': 'Three', 'tracknumber': '3', 'tracktotal': '3'},
    }
    monkeypatch.setattr(app_module, 'read_audio_tags',
                        lambda fp: tags[os.path.basename(fp)])

    data = _tracks(client, rel).get_json()
    assert data['completeness']['status'] == 'incomplete'
    assert data['completeness']['missing_count'] == 1
    rows = data['tracks']
    assert [t['tracknumber'] for t in rows] == [1, 2, 3]
    assert rows[1]['missing'] is True
    assert rows[1]['title'] is None  # a missing track has no title on disk


def test_album_endpoint_trailing_gap_rows(client, library, monkeypatch):
    rel = os.path.relpath(
        _make_album(library, 'Artist', 'Album', ['t1.flac']),
        library,
    )
    monkeypatch.setattr(app_module, 'read_audio_tags',
                        lambda fp: {'title': 'One', 'tracknumber': '1', 'tracktotal': '3'})

    rows = _tracks(client, rel).get_json()['tracks']
    assert [t['tracknumber'] for t in rows] == [1, 2, 3]
    assert [t['missing'] for t in rows] == [False, True, True]


def test_album_endpoint_multi_disc_missing_against_disc(client, library, monkeypatch):
    rel = os.path.relpath(
        _make_album(library, 'Artist', 'Album',
                    ['d1t1.flac', 'd1t2.flac', 'd2t1.flac']),
        library,
    )
    tags = {
        'd1t1.flac': {'tracknumber': '1', 'discnumber': '1', 'tracktotal': '2', 'disctotal': '2'},
        'd1t2.flac': {'tracknumber': '2', 'discnumber': '1', 'tracktotal': '2', 'disctotal': '2'},
        'd2t1.flac': {'tracknumber': '1', 'discnumber': '2', 'tracktotal': '2', 'disctotal': '2'},
    }
    monkeypatch.setattr(app_module, 'read_audio_tags',
                        lambda fp: tags[os.path.basename(fp)])

    data = _tracks(client, rel).get_json()
    assert data['completeness']['status'] == 'incomplete'
    # Disc 2 track 2 missing, reported against disc 2.
    assert data['completeness']['missing'] == [{'disc': 2, 'track': 2}]
    gap = [t for t in data['tracks'] if t['missing']]
    assert len(gap) == 1
    assert gap[0]['discnumber'] == 2 and gap[0]['tracknumber'] == 2


def test_album_endpoint_no_tags_is_unknown(client, library, monkeypatch):
    rel = os.path.relpath(
        _make_album(library, 'Artist', 'Album', ['x.flac']),
        library,
    )
    monkeypatch.setattr(app_module, 'read_audio_tags', lambda fp: {})

    data = _tracks(client, rel).get_json()
    assert data['completeness']['status'] == 'unknown'
    # No total known -> no gap rows are invented.
    assert all(t['missing'] is False for t in data['tracks'])


def test_assessment_cached_on_folder_mtime(client, library, monkeypatch):
    # Re-expanding an unchanged album must not re-read tags: the per-album
    # assessment is cached keyed on the folder mtime (ADR-0003).
    rel = os.path.relpath(
        _make_album(library, 'Artist', 'Album', ['a.flac', 'b.flac']),
        library,
    )
    app_module.album_assessment_cache.clear()

    calls = {'n': 0}

    def counting_reader(fp):
        calls['n'] += 1
        return {'title': 'T', 'tracknumber': '1', 'tracktotal': '2'}

    monkeypatch.setattr(app_module, 'read_audio_tags', counting_reader)

    _tracks(client, rel)
    first = calls['n']
    assert first > 0

    # Second request on the unchanged folder: served from cache, no new reads.
    _tracks(client, rel)
    assert calls['n'] == first


def test_assessment_cache_invalidated_when_folder_changes(client, library, monkeypatch):
    # Deleting/adding a track changes the folder mtime, which must invalidate the
    # cached assessment so completeness reflects the new contents.
    album_dir = _make_album(library, 'Artist', 'Album', ['a.flac', 'b.flac'])
    rel = os.path.relpath(album_dir, library)
    app_module.album_assessment_cache.clear()

    tags = {
        'a.flac': {'title': 'One', 'tracknumber': '1', 'tracktotal': '2'},
        'b.flac': {'title': 'Two', 'tracknumber': '2', 'tracktotal': '2'},
    }
    monkeypatch.setattr(app_module, 'read_audio_tags',
                        lambda fp: tags[os.path.basename(fp)])

    assert _tracks(client, rel).get_json()['completeness']['status'] == 'complete'

    # Delete track 2 and bump the folder mtime so the cache key changes.
    os.remove(os.path.join(album_dir, 'b.flac'))
    future = os.stat(album_dir).st_mtime + 10
    os.utime(album_dir, (future, future))

    data = _tracks(client, rel).get_json()
    assert data['completeness']['status'] == 'incomplete'
    assert data['completeness']['missing'] == [{'disc': 1, 'track': 2}]


def test_disc_number_orders_before_track_number(client, library, monkeypatch):
    rel = os.path.relpath(
        _make_album(library, 'Artist', 'Album', ['d2t1.flac', 'd1t2.flac', 'd1t1.flac']),
        library,
    )
    tags = {
        'd2t1.flac': {'title': 'D2T1', 'discnumber': '2', 'tracknumber': '1'},
        'd1t2.flac': {'title': 'D1T2', 'discnumber': '1', 'tracknumber': '2'},
        'd1t1.flac': {'title': 'D1T1', 'discnumber': '1', 'tracknumber': '1'},
    }
    monkeypatch.setattr(
        app_module, 'read_audio_tags',
        lambda fp: tags[os.path.basename(fp)],
    )

    tracks = _tracks(client, rel).get_json()['tracks']
    assert [t['title'] for t in tracks] == ['D1T1', 'D1T2', 'D2T1']
