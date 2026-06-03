"""Terminal-state classification of a finished rip run (pure function)."""
from app import classify_download

SKIP_LINE = (
    "Skipping track 1: 'Song'. "
    "Marked as downloaded in the database."
)


def test_nonzero_exit_is_failed():
    assert classify_download(1, "anything") == 'failed'


def test_completed_when_downloading_happened():
    output = "─ Downloading track 1\nDone"
    assert classify_download(0, output) == 'completed'


def test_skipped_when_only_skip_lines_and_nothing_downloaded():
    output = f"{SKIP_LINE}\n{SKIP_LINE}\nFetching cover art"
    assert classify_download(0, output) == 'skipped'


def test_not_skipped_when_some_tracks_downloaded():
    # Mixed run: some skipped, some downloaded -> completed, not skipped.
    output = f"{SKIP_LINE}\n─ Downloading track 2"
    assert classify_download(0, output) == 'completed'


def test_plain_success_with_no_skip_line_is_completed():
    assert classify_download(0, "Resolving metadata\nDone") == 'completed'
