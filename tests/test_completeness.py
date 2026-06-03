"""Pure Album-completeness assessment (ADR-0003).

``assess_completeness`` takes plain tag data — ``disc``/``track``/``tracktotal``
/``disctotal`` per present track — and returns Complete / Incomplete (with the
missing numbers per disc) / Unknown, with no filesystem, network, or mutagen
involvement. Any present track reveals its disc's true total, so every gap —
interior and trailing — is detectable from disk alone.
"""
import app as app_module

assess = app_module.assess_completeness


def test_complete_album_when_every_expected_track_present():
    result = assess([
        {'track': '1', 'tracktotal': '3'},
        {'track': '2', 'tracktotal': '3'},
        {'track': '3', 'tracktotal': '3'},
    ])
    assert result['status'] == 'complete'
    assert result['missing'] == []


def test_interior_gap_is_detected():
    result = assess([
        {'track': '1', 'tracktotal': '3'},
        {'track': '3', 'tracktotal': '3'},
    ])
    assert result['status'] == 'incomplete'
    assert result['missing'] == [{'disc': 1, 'track': 2}]


def test_trailing_gap_is_detected():
    # Only track 1 of 3 present: tracks 2 and 3 are trailing gaps, detectable
    # only because a present track reveals the true total of 3.
    result = assess([
        {'track': '1', 'tracktotal': '3'},
    ])
    assert result['status'] == 'incomplete'
    assert result['missing'] == [{'disc': 1, 'track': 2}, {'disc': 1, 'track': 3}]


def test_multi_disc_gap_reported_against_its_disc():
    result = assess([
        {'disc': '1', 'track': '1', 'tracktotal': '2', 'disctotal': '2'},
        {'disc': '1', 'track': '2', 'tracktotal': '2', 'disctotal': '2'},
        {'disc': '2', 'track': '1', 'tracktotal': '3', 'disctotal': '2'},
        {'disc': '2', 'track': '3', 'tracktotal': '3', 'disctotal': '2'},
    ])
    assert result['status'] == 'incomplete'
    # Disc 1 complete; disc 2 missing track 2 — reported against disc 2.
    assert result['missing'] == [{'disc': 2, 'track': 2}]


def test_multi_disc_complete():
    result = assess([
        {'disc': '1', 'track': '1', 'tracktotal': '2'},
        {'disc': '1', 'track': '2', 'tracktotal': '2'},
        {'disc': '2', 'track': '1', 'tracktotal': '2'},
        {'disc': '2', 'track': '2', 'tracktotal': '2'},
    ])
    assert result['status'] == 'complete'
    assert result['missing'] == []


def test_zero_readable_tags_is_unknown_not_guessed():
    # No present track carries a readable total: completeness cannot be
    # determined, so it is Unknown rather than guessed from the sequence.
    result = assess([
        {'track': None},
        {'track': None},
    ])
    assert result['status'] == 'unknown'
    assert result['missing'] == []


def test_empty_album_is_unknown():
    assert assess([])['status'] == 'unknown'


def test_present_numbers_without_total_is_unknown():
    # Track numbers present but no tracktotal anywhere -> can't know the true
    # total, so Unknown (cannot detect trailing gaps).
    result = assess([
        {'track': '1'},
        {'track': '2'},
    ])
    assert result['status'] == 'unknown'


def test_track_beyond_tagged_total_is_never_reported_missing():
    # A present track number larger than the tagged total must not be reported
    # missing; trust the highest present number.
    result = assess([
        {'track': '1', 'tracktotal': '2'},
        {'track': '2', 'tracktotal': '2'},
        {'track': '3', 'tracktotal': '2'},
    ])
    assert result['status'] == 'complete'
    assert result['missing'] == []


def test_track_with_no_disc_tag_is_disc_one():
    result = assess([
        {'track': '1', 'tracktotal': '2'},
        {'track': '2', 'tracktotal': '2'},
    ])
    assert result['discs'][0]['disc'] == 1
    assert result['status'] == 'complete'


def test_slashed_totals_are_parsed():
    # streamrip-style "1/3" tracknumber/tracktotal forms parse to the leading int.
    result = assess([
        {'track': '1/3', 'tracktotal': '3/3'},
    ])
    assert result['status'] == 'incomplete'
    assert result['missing'] == [{'disc': 1, 'track': 2}, {'disc': 1, 'track': 3}]
