"""Integration tests for the mutagen tag-reading adapter (ADR-0003) against real
tagged sample files on disk — not the stubbed seam.

streamrip writes tracknumber/tracktotal/discnumber/disctotal into every file;
these tests build genuine FLAC files (silent, encoded with the system `flac`),
tag them with mutagen exactly as streamrip would, and assert the production
adapter (``app.read_audio_tags``, i.e. the real mutagen reader) reads them back
and that the whole album endpoint then assesses completeness from those tags.

If the system `flac` encoder or mutagen is unavailable the module is skipped.
"""
import os
import shutil
import struct
import subprocess
import wave

import pytest

import app as app_module

pytest.importorskip("mutagen")
from mutagen.flac import FLAC  # noqa: E402

if shutil.which("flac") is None:
    pytest.skip("system `flac` encoder not available", allow_module_level=True)


def _write_flac(path, **tags):
    """Encode a short silent FLAC at ``path`` and write the given Vorbis tags
    (the same fields streamrip embeds)."""
    wav_path = path + ".wav"
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<800h", *([0] * 800)))
    subprocess.run(
        ["flac", "--totally-silent", "-f", "-o", path, wav_path],
        check=True,
    )
    os.remove(wav_path)
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key] = str(value)
    audio.save()


def test_real_flac_tags_are_read_by_production_adapter(tmp_path):
    path = str(tmp_path / "01.flac")
    _write_flac(
        path,
        title="Opener",
        tracknumber="1",
        tracktotal="3",
        discnumber="1",
        disctotal="1",
    )
    # The production reader (real mutagen), not the test seam.
    tags = app_module._default_tag_reader(path)
    assert tags["title"] == "Opener"
    assert tags["tracknumber"] == "1"
    assert tags["tracktotal"] == "3"


def test_album_endpoint_assesses_real_tagged_files(client, tmp_path, monkeypatch):
    base = str(tmp_path / "Music")
    album_dir = os.path.join(base, "Artist", "Album")
    os.makedirs(album_dir, exist_ok=True)
    # A 3-track album with track 2 missing on disk -> INCOMPLETE (1 missing).
    _write_flac(os.path.join(album_dir, "01.flac"),
                title="One", tracknumber="1", tracktotal="3")
    _write_flac(os.path.join(album_dir, "03.flac"),
                title="Three", tracknumber="3", tracktotal="3")
    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", base)
    # Use the real mutagen adapter (the seam is left at its production default).

    resp = client.get("/api/library/album", query_string={"path": "Artist/Album"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["completeness"]["status"] == "incomplete"
    assert data["completeness"]["missing_count"] == 1
    assert data["completeness"]["missing"] == [{"disc": 1, "track": 2}]

    # The expected sequence renders the gap row at position 2.
    rows = data["tracks"]
    assert [t["tracknumber"] for t in rows] == [1, 2, 3]
    assert rows[1]["missing"] is True
    assert rows[0]["missing"] is False and rows[2]["missing"] is False


def test_album_with_no_readable_tags_is_unknown(client, tmp_path, monkeypatch):
    base = str(tmp_path / "Music")
    album_dir = os.path.join(base, "Artist", "Album")
    os.makedirs(album_dir, exist_ok=True)
    # A FLAC with no completeness tags at all (only a title).
    _write_flac(os.path.join(album_dir, "mystery.flac"), title="Mystery")
    monkeypatch.setattr(app_module, "DOWNLOAD_DIR", base)

    resp = client.get("/api/library/album", query_string={"path": "Artist/Album"})
    data = resp.get_json()
    assert data["completeness"]["status"] == "unknown"
