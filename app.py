import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import namedtuple

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)

# new logging config
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Defaults work for a local run; Docker overrides these via docker-compose environment
STREAMRIP_CONFIG = os.environ.get(
    "STREAMRIP_CONFIG", os.path.expanduser("~/.config/streamrip/config.toml")
)
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.expanduser("~/Downloads"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))

# Fail loudly at startup if the download dir is unusable, instead of silently per-download
try:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    if not os.access(DOWNLOAD_DIR, os.W_OK):
        raise PermissionError(f"directory is not writable")
except OSError as e:
    logger.error("=" * 60)
    logger.error(f"DOWNLOAD_DIR '{DOWNLOAD_DIR}' is not usable: {e}")
    logger.error(
        "All downloads WILL FAIL. Set the DOWNLOAD_DIR environment variable to a writable directory."
    )
    logger.error("=" * 60)

download_queue = queue.Queue()
# Server is the source of truth (ADR-0002): Active + History live here in memory.
# active_downloads maps task_id -> Download record (status: queued | downloading).
active_downloads = {}
active_lock = threading.Lock()
download_history = []
history_lock = threading.Lock()
sse_clients = []
album_art_cache = {}
cache_lock = threading.Lock()
MAX_HISTORY = 50

# The exact line streamrip logs once per track it refuses to re-download because the
# track is recorded in its own database. Skip detection keys on this line.
SKIP_LINE_RE = re.compile(r"Marked as downloaded in the database")

# The audio file extensions streamrip writes. Used to tell tracks apart from the
# non-audio files (cover art, logs) that share an album folder, so the Library
# never lists cover.jpg as a track.
AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".alac")


def _default_tag_reader(filepath):
    """Read embedded tags from one audio file with mutagen, returning the raw
    tracknumber/title (and the disc/total fields a later completeness slice
    needs). This is the seam tests replace with a fake so the Library tests
    never touch real files or require mutagen.

    streamrip writes these tags into every file (ADR-0003); mutagen reads them
    back. Returns {} when the file has no readable tags."""
    from mutagen import File as MutagenFile

    audio = MutagenFile(filepath, easy=True)
    if audio is None or audio.tags is None:
        return {}

    def first(key):
        value = audio.tags.get(key)
        if isinstance(value, list):
            return value[0] if value else None
        return value

    return {
        "title": first("title"),
        "tracknumber": first("tracknumber"),
        "discnumber": first("discnumber"),
        "tracktotal": first("tracktotal"),
        "disctotal": first("disctotal"),
    }


# Injectable tag-reading boundary (mirrors run_rip). Tests swap this for a fake
# that returns canned tags so the Library suite needs neither mutagen nor real
# audio files; production reads tags off disk with mutagen.
read_audio_tags = _default_tag_reader


def _is_audio_file(filename):
    return filename.lower().endswith(AUDIO_EXTENSIONS)


def _parse_track_number(raw):
    """Coerce a tag tracknumber/discnumber into an int. Tags are often stored as
    "7" or "7/12"; we take the leading integer and ignore anything unparseable."""
    if raw is None:
        return None
    match = re.match(r"\s*(\d+)", str(raw))
    return int(match.group(1)) if match else None


def assess_completeness(present_tracks):
    """Assess an album's Album completeness (ADR-0003 / CONTEXT glossary) purely
    from the embedded tags of the tracks *present* on disk — no network, no
    stored tracklist. ``present_tracks`` is a list of plain dicts with raw tag
    fields ``disc``/``track``/``tracktotal``/``disctotal`` (any may be missing).

    Returns a dict::

        {
          'status': 'complete' | 'incomplete' | 'unknown',
          'missing': [{'disc': D, 'track': N}, ...],   # locatable gaps, sorted
          'missing_discs': [D, ...],                   # discs with no tracks at all
          'unlocated': N,   # absent tracks whose disc/number cannot be determined
          'discs': [{'disc': D, 'present': [...], 'missing': [...]}, ...],
        }

    Tag semantics (verified against streamrip's tagger and real rips):
    ``tracktotal`` is the ALBUM-wide track count while ``tracknumber`` restarts
    per disc, and ``disctotal`` reveals how many discs the album has. So:

    - Single-disc album: every gap — interior and trailing — is locatable
      (expected sequence is 1…tracktotal).
    - Multi-disc album: interior gaps are locatable per disc from the numbering;
      the exact number of absent tracks is album total minus present count; but a
      trailing gap cannot be attributed to a specific disc — those are counted in
      ``unlocated``, never guessed. A disc with zero present tracks is a wholly
      Missing disc (reported in ``missing_discs``; its tracks count toward
      ``unlocated``).

    An album with no present track carrying a readable ``tracktotal`` is
    **Unknown** (never guessed); a track with no readable disc is disc 1."""
    discs = {}
    saw_total = False
    album_total = 0
    disctotal_seen = 0

    for raw in present_tracks:
        disc = _parse_track_number(raw.get("disc"))
        track = _parse_track_number(raw.get("track"))
        tracktotal = _parse_track_number(raw.get("tracktotal"))
        disctotal = _parse_track_number(raw.get("disctotal"))

        # A track with no readable disc tag belongs to disc 1 (the common
        # single-disc case where streamrip omits/zeroes the disc number).
        if disc is None or disc < 1:
            disc = 1
        if disctotal is not None and disctotal > disctotal_seen:
            disctotal_seen = disctotal
        if tracktotal is not None and tracktotal >= 1:
            saw_total = True
            album_total = max(album_total, tracktotal)

        present = discs.setdefault(disc, set())
        if track is not None and track >= 1:
            present.add(track)

    if not saw_total:
        return {
            "status": "unknown",
            "missing": [],
            "missing_discs": [],
            "unlocated": 0,
            "discs": [],
        }

    disctotal_seen = max(disctotal_seen, max(discs, default=1), 1)
    missing_discs = [d for d in range(1, disctotal_seen + 1) if d not in discs]
    present_count = sum(len(p) for p in discs.values())
    multi_disc = disctotal_seen > 1

    disc_reports = []
    located = []
    for disc in sorted(discs):
        present = sorted(discs[disc])
        highest = present[-1] if present else 0
        if multi_disc:
            # Per-disc totals are unknowable (tracktotal is album-wide), so only
            # interior gaps — numbers below the highest present — are locatable.
            expected_here = highest
        else:
            # Single disc: tracktotal IS this disc's total, so trailing gaps are
            # locatable too. Never report a present track as missing when its
            # number exceeds the tagged total.
            expected_here = max(album_total, highest)
        missing_here = [n for n in range(1, expected_here + 1) if n not in discs[disc]]
        disc_reports.append({"disc": disc, "present": present, "missing": missing_here})
        located.extend({"disc": disc, "track": n} for n in missing_here)

    located.sort(key=lambda m: (m["disc"], m["track"]))
    # The album-wide total makes the number of absent tracks exact even when
    # their positions are not; whatever the locatable gaps do not account for is
    # unlocated (trailing tracks on some disc, or the contents of a missing disc).
    shortfall = max(0, album_total - present_count)
    unlocated = max(0, shortfall - len(located))

    complete = not located and not missing_discs and not unlocated
    return {
        "status": "complete" if complete else "incomplete",
        "missing": located,
        "missing_discs": missing_discs,
        "unlocated": unlocated,
        "discs": disc_reports,
    }


DISC_FOLDER_RE = re.compile(r"^(disc|disk|cd)[ _-]*\d+$", re.IGNORECASE)


def _is_disc_folder(name):
    """streamrip puts a multi-disc album's audio in "Disc N" subfolders
    (streamrip media/track.py); such a folder is part of its parent album, never
    an album of its own."""
    return bool(DISC_FOLDER_RE.match(name.strip()))


def list_library_albums(download_dir):
    """List the album folders in the Library (the albums on disk, ADR/Library
    glossary) without reading a single tag, so it stays instant on large
    libraries.

    streamrip's default folder_format puts album folders FLAT at the download
    root ("Artist - Album (Year) [...]"), with multi-disc audio in "Disc N"
    subfolders. So: an album folder is a directory that directly contains audio
    OR whose "Disc N" subfolders do; a disc folder folds into its parent album.
    The Artist is the album folder's parent directory when one exists (nested
    Artist/Album folder_format), otherwise None — the frontend renders
    root-level albums ungrouped, since their folder name already carries the
    artist.

    Returns a list of {artist, album, path} sorted by artist then album, where
    ``path`` is the album folder relative to ``download_dir`` (the handle the
    per-album endpoint expands)."""
    albums = {}
    if not os.path.isdir(download_dir):
        return []

    for root, dirs, filenames in os.walk(download_dir):
        dirs.sort()
        if not any(_is_audio_file(name) for name in filenames):
            continue

        album_dir = root
        # Audio inside a "Disc N" folder belongs to the parent album folder.
        if _is_disc_folder(os.path.basename(album_dir)):
            album_dir = os.path.dirname(album_dir)
            if os.path.realpath(album_dir) == os.path.realpath(download_dir):
                album_dir = root  # a bare "Disc 1" at the root: treat as album

        rel_path = os.path.relpath(album_dir, download_dir)
        if rel_path in albums:
            continue
        parent = os.path.dirname(rel_path)
        albums[rel_path] = {
            "artist": os.path.basename(parent) if parent and parent != "." else None,
            "album": os.path.basename(album_dir),
            "path": rel_path,
        }

    return sorted(
        albums.values(),
        key=lambda a: ((a["artist"] or "").lower(), a["album"].lower()),
    )


def read_album_tracks(download_dir, rel_path):
    """Read one album folder's present tracks, lazily — called only when the user
    expands an album. Each audio file's title and track number come from its
    embedded tags (ADR-0003) via the read_audio_tags seam; non-audio files (cover
    art, etc.) are skipped so they are never listed as tracks.

    Returns a list of {tracknumber, discnumber, title, filename} sorted by
    (disc, track). A file whose tags are unreadable still appears (its title
    falls back to the filename) so present tracks are never hidden."""
    album_dir = os.path.join(download_dir, rel_path)
    tracks = []
    if not os.path.isdir(album_dir):
        return tracks

    # The album's audio sits directly in the folder, plus inside any "Disc N"
    # subfolders for multi-disc albums (streamrip's layout). Track filenames keep
    # their disc subfolder prefix so they stay unique within the album.
    audio_files = []
    for entry in os.listdir(album_dir):
        entry_path = os.path.join(album_dir, entry)
        if os.path.isfile(entry_path) and _is_audio_file(entry):
            audio_files.append(entry)
        elif os.path.isdir(entry_path) and _is_disc_folder(entry):
            for sub in os.listdir(entry_path):
                if _is_audio_file(sub) and os.path.isfile(os.path.join(entry_path, sub)):
                    audio_files.append(os.path.join(entry, sub))

    for filename in audio_files:
        filepath = os.path.join(album_dir, filename)
        try:
            tags = read_audio_tags(filepath) or {}
        except Exception as e:
            logger.warning(f"Failed to read tags from {filepath}: {e}")
            tags = {}

        tracknumber = _parse_track_number(tags.get("tracknumber"))
        discnumber = _parse_track_number(tags.get("discnumber"))
        title = tags.get("title") or filename
        tracks.append(
            {
                "tracknumber": tracknumber,
                "discnumber": discnumber,
                "title": title,
                "filename": filename,
                # Carry the raw totals through so completeness can be assessed from
                # the same tag read (ADR-0003) without a second pass over the files.
                "tracktotal": _parse_track_number(tags.get("tracktotal")),
                "disctotal": _parse_track_number(tags.get("disctotal")),
            }
        )

    tracks.sort(
        key=lambda t: (
            t["discnumber"] if t["discnumber"] is not None else 0,
            t["tracknumber"] if t["tracknumber"] is not None else 0,
            t["filename"].lower(),
        )
    )
    return tracks


# Per-album completeness assessment cache, keyed on the album folder path and its
# mtime (ADR-0003: completeness comes from the tags on disk). Re-expanding an
# unchanged album returns the cached payload without re-reading any tags;
# downloading/deleting a track changes the folder mtime and invalidates it.
album_assessment_cache = {}
album_cache_lock = threading.Lock()


def _album_folder_mtime(album_dir):
    """The newest mtime across the album folder and its "Disc N" subfolders —
    bumped by the filesystem whenever a track file is added to or removed from
    any of them, which is exactly the events that can change completeness.
    (A deletion inside "Disc 2" does not touch the parent folder's mtime, so the
    subfolders must participate in the cache key.) Returns None when the folder
    is missing."""
    try:
        mtime = os.stat(album_dir).st_mtime
        for entry in os.listdir(album_dir):
            entry_path = os.path.join(album_dir, entry)
            if _is_disc_folder(entry) and os.path.isdir(entry_path):
                mtime = max(mtime, os.stat(entry_path).st_mtime)
        return mtime
    except OSError:
        return None


def _build_track_rows(present_tracks, assessment):
    """Merge the present tracks with greyed "missing" gap rows so the expanded
    album renders the full expected sequence 1…tracktotal per disc, with absent
    tracks shown in sequence position (interior and trailing). A missing track is
    shown by number only — it left no title on disk (ADR-0003)."""
    rows = list(present_tracks)
    for miss in assessment.get("missing", []):
        rows.append(
            {
                "tracknumber": miss["track"],
                "discnumber": miss["disc"],
                "title": None,
                "filename": None,
                "tracktotal": None,
                "disctotal": None,
                "missing": True,
            }
        )
    # A wholly Missing disc renders as a single gap row for the disc — its track
    # count is unknown, so there are no per-track rows to merge.
    for disc in assessment.get("missing_discs", []):
        rows.append(
            {
                "tracknumber": None,
                "discnumber": disc,
                "title": None,
                "filename": None,
                "tracktotal": None,
                "disctotal": None,
                "missing": True,
                "missing_disc": True,
            }
        )
    # Absent tracks whose position is unknowable (trailing gaps on a multi-disc
    # album: tracktotal is album-wide, so they cannot be pinned to a disc) render
    # as one summary gap row at the end of the sequence.
    if assessment.get("unlocated"):
        rows.append(
            {
                "tracknumber": None,
                "discnumber": None,
                "title": None,
                "filename": None,
                "tracktotal": None,
                "disctotal": None,
                "missing": True,
                "unlocated_count": assessment["unlocated"],
            }
        )
    # Sort present and missing rows into one expected sequence. A present track
    # with no disc tag belongs to disc 1, exactly as assess_completeness assigns
    # its gap rows, so the two interleave correctly (no disc tag -> disc 1). The
    # unlocated summary row belongs to no disc and sorts after everything.
    def _row_key(t):
        if t.get("unlocated_count"):
            return (float("inf"), 0, "")
        return (
            t["discnumber"]
            if t["discnumber"] is not None and t["discnumber"] >= 1
            else 1,
            t["tracknumber"] if t["tracknumber"] is not None else 0,
            (t.get("filename") or "").lower(),
        )

    rows.sort(key=_row_key)
    for row in rows:
        row.setdefault("missing", False)
    return rows


def get_album_assessment(download_dir, rel_path):
    """Read one album's present tracks, assess its Album completeness, and return
    the payload the per-album endpoint serves: the full track sequence (present
    tracks plus greyed missing gap rows) and the completeness badge data.

    Cached per album keyed on the folder's mtime (ADR-0003): re-expanding an
    unchanged album returns the cached result and does not re-read a single tag."""
    album_dir = os.path.join(download_dir, rel_path)
    mtime = _album_folder_mtime(album_dir)

    cache_key = os.path.realpath(album_dir)
    with album_cache_lock:
        cached = album_assessment_cache.get(cache_key)
        if cached is not None and cached["mtime"] == mtime and mtime is not None:
            return cached["payload"]

    present_tracks = read_album_tracks(download_dir, rel_path)
    assessment = assess_completeness(
        [
            {
                "disc": t["discnumber"],
                "track": t["tracknumber"],
                "tracktotal": t["tracktotal"],
                "disctotal": t["disctotal"],
            }
            for t in present_tracks
        ]
    )
    payload = {
        "tracks": _build_track_rows(present_tracks, assessment),
        "completeness": {
            "status": assessment["status"],
            # The album-wide tracktotal makes the absent-track count exact even
            # when some positions are unlocatable (multi-disc trailing gaps).
            "missing_count": len(assessment["missing"]) + assessment["unlocated"],
            "missing": assessment["missing"],
            "missing_discs": assessment["missing_discs"],
            "unlocated": assessment["unlocated"],
        },
    }

    if mtime is not None:
        with album_cache_lock:
            album_assessment_cache[cache_key] = {"mtime": mtime, "payload": payload}
    return payload


def build_rip_command(
    url, quality, *, config_path=None, download_dir=None, no_db=False
):
    """Construct the `rip url` argv. Pure (no side effects) so it can be tested
    directly and reused for Redownload (no_db -> --no-db). This is the single
    place the download invocation is assembled."""
    cmd = ["rip"]
    if config_path and os.path.exists(config_path):
        cmd.extend(["--config-path", config_path])
    if no_db:
        cmd.append("--no-db")
    if download_dir:
        cmd.extend(["-f", download_dir])
    cmd.extend(["-q", str(quality)])
    cmd.extend(["url", url])
    return cmd


def build_search_command(source, search_type, query, output_file, *, config_path=None):
    """Construct the `rip search` argv. Pure (no side effects) so it can be
    tested directly. Mirrors build_rip_command's config-path handling: the
    --config-path flag is only added when the config file exists. This is the
    single place the search invocation is assembled."""
    cmd = ["rip"]
    if config_path and os.path.exists(config_path):
        cmd.extend(["--config-path", config_path])
    cmd.extend(["search", "--output-file", output_file])
    cmd.extend([source, search_type, query])
    return cmd


def classify_download(returncode, output):
    """Map a finished `rip` run to a terminal Download state from its exit code
    and stdout. Pure, so the worker's terminal-state logic is unit-testable."""
    if returncode != 0:
        return "failed"
    # Skipped: rip did no downloading work because every track was already
    # recorded in its database. Keyed on the skip log line; only a skip when
    # nothing was actually downloaded.
    if SKIP_LINE_RE.search(output) and "─ Downloading" not in output:
        return "skipped"
    return "completed"


def classify_search_error(returncode, stdout):
    """Map a finished `rip search` run to a user-facing error message from its
    exit code and stdout. Pure (mirrors classify_download), so the route's error
    mapping has locality and a real test surface. Returns None on success."""
    if returncode == 0:
        return None
    error_msg = "Streamrip search failed"
    if stdout:
        if "InvalidAppSecretError" in stdout:
            error_msg = "Invalid Qobuz app secrets. Update your config with valid secrets or run 'rip config --update' in the container."
        elif "Traceback" in stdout:
            error_msg = (
                "Streamrip encountered an error (check logs for full traceback)"
            )
        elif "authentication" in stdout.lower():
            error_msg = (
                "Authentication failed - check your Qobuz credentials in config"
            )
        elif "credentials" in stdout.lower():
            error_msg = "Invalid credentials - check your Qobuz configuration"
    return error_msg


ParsedSearch = namedtuple("ParsedSearch", ["results", "error"])


def parse_search_results(content, source, search_type):
    """Turn the raw text `rip search --output-file` wrote into the result dicts
    the endpoint serves. Pure (no file IO, no subprocess), so the most
    correctness-sensitive part of Search — parsing an external program's output —
    is unit-testable directly rather than only through a live `rip` run.

    Returns a ParsedSearch(results, error):
      - success      -> (list_of_result_dicts, None)
      - empty/blank  -> ([], "empty")            # rip wrote nothing
      - malformed    -> ([], <JSONDecodeError>)  # rip wrote non-JSON
    Never raises on bad input; the endpoint maps `error` to its response. Owns
    the `desc` -> title/artist split and the construct_url wiring."""
    if not content or content.strip() == "":
        return ParsedSearch([], "empty")

    try:
        search_data = json.loads(content)
    except json.JSONDecodeError as e:
        return ParsedSearch([], e)

    results = []
    for item in search_data:
        item_id = item.get("id", "")
        media_type = item.get("media_type", search_type)
        url = construct_url(item.get("source", source), media_type, item_id)

        desc = item.get("desc", "")
        artist = ""
        title = desc
        if " by " in desc:
            parts = desc.rsplit(" by ", 1)
            title = parts[0]
            artist = parts[1]

        results.append(
            {
                "id": item_id,
                "service": item.get("source", source),
                "type": media_type,
                "artist": artist if artist else desc,
                "title": title if artist else "",
                "desc": desc,
                "url": url,
                "album_art": "",
            }
        )

    return ParsedSearch(results, None)


def _default_runner(cmd):
    """Run `rip` as a subprocess (ADR-0001), yielding stripped stdout lines and
    finally returning the exit code. This is the seam tests replace with a fake
    so no test ever launches a real `rip` process."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    try:
        for line in process.stdout:
            line = line.strip()
            if line:
                yield line
        process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
    return process.returncode


# Injectable subprocess boundary. Tests swap this for a fake generator that emits
# canned output and a return code; production uses the real `rip` runner.
run_rip = _default_runner


def _default_search_runner(cmd):
    """Run `rip search` as a subprocess (ADR-0001), returning the finished
    CompletedProcess (returncode/stdout/stderr). Search is one-shot — it writes
    its results to a file and exits — so unlike the streaming download runner
    this blocks and captures output in one go. This is the seam tests replace
    with a fake so no test ever launches a real `rip search` process."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


# Injectable subprocess boundary for Search (mirrors run_rip). Tests swap this
# for a fake returning a result with the same .returncode/.stdout/.stderr shape;
# production runs the real `rip search`.
run_search = _default_search_runner


def register_queued(task):
    """Register a submitted Download in server Active state as `queued` and
    announce it immediately, before any worker is free. Guarantees a submitted
    Download is visible the instant the server accepts it."""
    record = {
        "id": task["id"],
        "url": task["url"],
        "quality": task.get("quality", 3),
        "metadata": task.get("metadata", {}),
        "status": "queued",
        "queued_at": time.time(),
    }
    with active_lock:
        active_downloads[task["id"]] = record
    broadcast_sse(
        {
            "type": "download_queued",
            "id": record["id"],
            "url": record["url"],
            "quality": record["quality"],
            "metadata": record["metadata"],
            "status": "queued",
        }
    )


def enqueue_download(url, quality=3, metadata=None, no_db=False):
    """Create a Download, register it as Queued (visible immediately), and hand
    it to a worker. Returns the task id.

    ``no_db`` forces a Redownload: the worker's `rip` invocation gets --no-db so
    streamrip ignores its own database and downloads an already-recorded item
    again (see the Redownload glossary entry)."""
    task_id = f"dl_{int(time.time() * 1000)}_{len(active_downloads)}"
    task = {
        "id": task_id,
        "url": url,
        "quality": quality,
        "metadata": metadata or {},
        "no_db": no_db,
    }
    register_queued(task)
    download_queue.put(task)
    return task_id


class DownloadWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.current_process = None

    def run(self):
        while True:
            task = download_queue.get()
            if task is None:
                break

            task_id = task["id"]
            url = task["url"]
            quality = task.get("quality", 3)
            metadata = task.get("metadata", {})
            no_db = task.get("no_db", False)

            # Transition the existing Queued card in place to Downloading.
            with active_lock:
                record = active_downloads.get(task_id)
                if record is not None:
                    record["status"] = "downloading"
                    record["started"] = time.time()

            broadcast_sse(
                {
                    "type": "download_started",
                    "id": task_id,
                    "url": url,
                    "quality": quality,
                    "metadata": metadata,
                    "status": "downloading",
                }
            )

            output_lines = []
            cmd = build_rip_command(
                url,
                quality,
                config_path=STREAMRIP_CONFIG,
                download_dir=DOWNLOAD_DIR,
                no_db=no_db,
            )

            try:
                runner = run_rip(cmd)
                returncode = 0
                try:
                    while True:
                        line = next(runner)
                        if line:
                            output_lines.append(line)
                            if len(output_lines) % 10 == 0:
                                broadcast_sse(
                                    {
                                        "type": "download_progress",
                                        "id": task_id,
                                        "output": "\n".join(output_lines[-5:]),
                                        "progress": {"raw_output": True},
                                    }
                                )
                except StopIteration as stop:
                    returncode = stop.value if stop.value is not None else 0

                full_output = "\n".join(output_lines)
                status = classify_download(returncode, full_output)

                if status == "failed":
                    logger.error(
                        f"Download failed (exit code {returncode}): {' '.join(cmd)}"
                    )
                    logger.error(
                        "rip output (last 30 lines):\n%s", "\n".join(output_lines[-30:])
                    )
                elif status == "skipped":
                    logger.info(
                        f"Already downloaded (marked in streamrip database): {url}"
                    )

                finalize_download(task_id, status, metadata, full_output)

            except Exception as e:
                logger.exception(f"Download worker error for {url}")
                finalize_download(
                    task_id,
                    "failed",
                    metadata,
                    "\n".join(output_lines) if output_lines else str(e),
                    error=str(e),
                )

            finally:
                self.current_process = None

            download_queue.task_done()


def finalize_download(task_id, status, metadata, output, error=None):
    """Move a Download out of Active into server-owned History with its terminal
    state, and broadcast the transition. History is appended server-side here so
    it survives a page refresh (ADR-0002).

    The History entry retains the Download's original URL and quality (taken from
    the Active record) alongside its metadata and final status, because the
    Redownload slice re-runs a History entry from exactly those fields."""
    with active_lock:
        record = active_downloads.pop(task_id, None)

    entry = {
        "id": task_id,
        "url": record.get("url") if record else None,
        "quality": record.get("quality") if record else None,
        "metadata": metadata or (record.get("metadata") if record else {}),
        "status": status,
        "output": output,
        "completed_at": time.time(),
    }
    if error:
        entry["error"] = error

    with history_lock:
        download_history.insert(0, entry)
        del download_history[MAX_HISTORY:]

    broadcast_sse(
        {
            "type": "download_completed",
            "id": task_id,
            "url": entry["url"],
            "quality": entry["quality"],
            "status": status,
            "metadata": entry["metadata"],
            "output": output,
            **({"error": error} if error else {}),
        }
    )


def broadcast_sse(data):
    message = f"data: {json.dumps(data)}\n\n"
    dead_clients = []

    for client in sse_clients:
        try:
            client.put(message)
        except:
            dead_clients.append(client)

    for client in dead_clients:
        sse_clients.remove(client)


@app.route("/api/events")
def sse_events():
    def generate():
        q = queue.Queue()
        sse_clients.append(q)

        try:
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"

            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    continue  # previous heartbeat check
        finally:
            sse_clients.remove(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


workers = []
for _ in range(MAX_CONCURRENT_DOWNLOADS):
    worker = DownloadWorker()
    worker.start()
    workers.append(worker)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    # Service worker must be served from the root so its scope covers the whole app
    return app.send_static_file("sw.js")


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url")
    quality = data.get("quality", 3)

    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Validate URL (basic check)
    # youtube-dl for later
    valid_services = [
        "spotify.com",
        "deezer.com",
        "tidal.com",
        "qobuz.com",
        "soundcloud.com",
        "youtube.com",
    ]
    if not any(service in url.lower() for service in valid_services):
        return jsonify({"error": "Unsupported service URL"}), 400

    metadata = extract_metadata_from_url(url)

    task_id = enqueue_download(url, quality, metadata)

    return jsonify({"task_id": task_id, "status": "queued"})


@app.route("/api/status")
def get_all_status():
    # Server is the source of truth (ADR-0002). Active is returned as a list so the
    # frontend can rehydrate the unified Active list (Queued + Downloading) on load.
    with active_lock:
        active = list(active_downloads.values())
    with history_lock:
        history = list(download_history)
    return jsonify(
        {"active": active, "history": history, "queue_size": download_queue.qsize()}
    )


@app.route("/api/config", methods=["GET", "POST"])
def config():
    if request.method == "GET":
        if os.path.exists(STREAMRIP_CONFIG):
            with open(STREAMRIP_CONFIG, "r") as f:
                return jsonify({"config": f.read()})
        return jsonify({"config": ""})

    elif request.method == "POST":
        data = request.json
        config_content = data.get("config", "")

        try:
            if os.path.exists(STREAMRIP_CONFIG):
                shutil.copy2(STREAMRIP_CONFIG, f"{STREAMRIP_CONFIG}.bak")

            os.makedirs(os.path.dirname(STREAMRIP_CONFIG), exist_ok=True)
            with open(STREAMRIP_CONFIG, "w") as f:
                f.write(config_content)

            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route("/api/search", methods=["POST"])
def search_music():
    data = request.json
    query = data.get("query")
    search_type = data.get("type", "album")
    source = data.get("source", "qobuz")

    # new logging
    logger.info("=" * 60)
    logger.info("SEARCH REQUEST RECEIVED")
    logger.info(f"Query: '{query}'")
    logger.info(f"Type: {search_type}")
    logger.info(f"Source: {source}")
    logger.info("=" * 60)

    if not query:
        logger.warning("No query provided")
        return jsonify({"error": "Query required"}), 400

    if source == "soundcloud" and search_type in ["album", "artist"]:
        logger.debug(f"SoundCloud doesn't support {search_type} search")
        return jsonify(
            {
                "results": [],
                "query": query,
                "source": source,
                "total_count": 0,
                "message": f"SoundCloud does not support {search_type} searches. Try searching for tracks or playlists instead.",
            }
        )

    try:
        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".txt", delete=False
        ) as tmp_file:
            tmp_path = tmp_file.name

        logger.info(f"Created temp file: {tmp_path}")

        if os.path.exists(STREAMRIP_CONFIG):
            logger.info(f"Using config file: {STREAMRIP_CONFIG}")
        else:
            logger.warning(f"Config file not found at: {STREAMRIP_CONFIG}")

        cmd = build_search_command(
            source, search_type, query, tmp_path, config_path=STREAMRIP_CONFIG
        )

        logger.info(f"Executing command: {' '.join(cmd)}")

        result = run_search(cmd)

        logger.info(f"Command completed with return code: {result.returncode}")

        if result.stdout:
            logger.info(f"STDOUT ({len(result.stdout)} chars total):\n{result.stdout}")
        else:
            logger.info("STDOUT: (empty)")

        if result.stderr:
            logger.warning(
                f"STDERR ({len(result.stderr)} chars total):\n{result.stderr}"
            )
        else:
            logger.info("STDERR: (empty)")

        if result.returncode != 0:
            logger.error(
                f"Streamrip command failed with return code {result.returncode}"
            )
            error_msg = classify_search_error(result.returncode, result.stdout)

            return jsonify(
                {
                    "error": error_msg,
                    "debug_info": {
                        "return_code": result.returncode,
                        "stdout_preview": result.stdout
                        if result.stdout
                        else "",  # Send full output
                        "stderr_preview": result.stderr if result.stderr else "",
                        "command": " ".join(cmd),
                    },
                }
            ), 500

        # Check if temp file exists and has content
        if os.path.exists(tmp_path):
            file_size = os.path.getsize(tmp_path)
            logger.info(f"Temp file exists, size: {file_size} bytes")
        else:
            logger.error(f"Temp file does not exist: {tmp_path}")

        results = []

        try:
            with open(tmp_path, "r") as f:
                content = f.read()
                logger.info(f"Streamrip search output: {content[:500]}")
                logger.info(f"File content length: {len(content)} characters")
                logger.debug(f"File content (first 500 chars):\n{content[:500]}")

                parsed = parse_search_results(content, source, search_type)

                if parsed.error == "empty":
                    logger.warning("Temp file is empty!")
                    return jsonify(
                        {
                            "results": [],
                            "query": query,
                            "source": source,
                            "total_count": 0,
                            "message": "No results found. The search returned empty results.",
                            "debug_info": {
                                "return_code": result.returncode,
                                "stdout": result.stdout[:200] if result.stdout else "",
                                "stderr": result.stderr[:200] if result.stderr else "",
                            },
                        }
                    )

                if isinstance(parsed.error, json.JSONDecodeError):
                    e = parsed.error
                    logger.error("=" * 60)
                    logger.error("JSON PARSE ERROR")
                    logger.error(f"Error: {e}")
                    logger.error(f"Error position: line {e.lineno}, column {e.colno}")
                    logger.error(f"Content length: {len(content)} characters")
                    logger.error(f"Content type: {type(content)}")
                    logger.error(f"Content repr: {repr(content[:200])}")
                    logger.error("-" * 60)
                    logger.error(f"FULL CONTENT (all {len(content)} chars):")
                    logger.error(content)
                    logger.error("=" * 60)

                    # Also log what streamrip actually output
                    logger.error("STREAMRIP STDOUT:")
                    logger.error(result.stdout if result.stdout else "(empty)")
                    logger.error("-" * 60)
                    logger.error("STREAMRIP STDERR:")
                    logger.error(result.stderr if result.stderr else "(empty)")
                    logger.error("=" * 60)

                    return (
                        jsonify(
                            {
                                "error": "Failed to parse search results",
                                "debug_info": {
                                    "parse_error": str(e),
                                    "content_length": len(content),
                                    "content_preview": content[:500],
                                    "full_content": content,  # Include full content in response
                                    "stdout": result.stdout,
                                    "stderr": result.stderr,
                                },
                            }
                        ),
                        500,
                    )

                results = parsed.results
                logger.info(f"Successfully parsed JSON with {len(results)} items")
                for idx, result_item in enumerate(results[:3]):
                    logger.debug(f"Result {idx + 1}: {result_item}")

        except FileNotFoundError:
            logger.error(f"Temp file not found: {tmp_path}")
            return jsonify(
                {
                    "error": "Search output file not found",
                    "debug_info": {
                        "temp_path": tmp_path,
                        "return_code": result.returncode,
                    },
                }
            ), 500

        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                    logger.debug(f"Removed temp file: {tmp_path}")
                except Exception as e:
                    logger.warning(f"Failed to remove temp file: {e}")

        logger.info(f"Returning {len(results)} results")

        return jsonify(
            {
                "results": results,
                "query": query,
                "source": source,
                "total_count": len(results),
            }
        )

    except subprocess.TimeoutExpired:
        logger.error("Search command timed out after 30 seconds")
        return jsonify({"error": "Search timed out"}), 500
    except Exception as e:
        logger.exception(f"Unexpected error during search: {e}")
        return jsonify(
            {"error": str(e), "debug_info": {"exception_type": type(e).__name__}}
        ), 500


@app.route("/api/album-art", methods=["GET"])
def get_album_art():
    source = request.args.get("source")
    media_type = request.args.get("type")
    item_id = request.args.get("id")

    if not all([source, media_type, item_id]):
        return jsonify({"error": "Missing parameters"}), 400

    # Todo: handle SoundCloud special case and get correct albums if possible
    if source == "soundcloud":
        if "|" in item_id:
            item_id = item_id.split("|")[0]
        elif "soundcloud:tracks:" in item_id:
            match = re.search(r"soundcloud:tracks:(\d+)", item_id)
            if match:
                item_id = match.group(1)

    cache_key = f"{source}_{media_type}_{item_id}"
    if cache_key in album_art_cache:
        cached = album_art_cache[cache_key]
        if isinstance(cached, dict):
            return jsonify(cached)
        return jsonify({"album_art": cached})

    try:
        if source == "qobuz":
            result = fetch_single_album_art(item_id, media_type, None)
            album_art_cache[cache_key] = result
            return jsonify(
                {
                    "album_art": result.get("album_art", ""),
                    "tracks_count": result.get("tracks_count"),
                    "release_type": result.get("release_type"),
                    "year": result.get("year"),
                }
            )

        elif source == "tidal":
            if media_type == "artist":
                album_art = f"https://resources.tidal.com/images/{item_id}/750x750.jpg"
            else:
                album_art = f"https://resources.tidal.com/images/{item_id}/320x320.jpg"

            if album_art:
                album_art_cache[cache_key] = album_art
                return jsonify({"album_art": album_art})
            return jsonify({"album_art": ""})

        elif source == "deezer":
            if media_type == "artist":
                try:
                    response = requests.get(
                        f"https://api.deezer.com/artist/{item_id}", timeout=3
                    )
                    if response.status_code == 200:
                        data = response.json()
                        album_art = data.get("picture_medium", data.get("picture", ""))
                        if album_art:
                            album_art_cache[cache_key] = album_art
                            return jsonify({"album_art": album_art})
                except:
                    pass
                return jsonify({"album_art": ""})
            else:
                album_art = f"https://api.deezer.com/{media_type}/{item_id}/image"
                if album_art:
                    album_art_cache[cache_key] = album_art
                    return jsonify({"album_art": album_art})
                return jsonify({"album_art": ""})

        elif source == "soundcloud":
            # SoundCloud doesn't provide easy access to artwork
            # Just return empty and let the frontend handle placeholders
            return jsonify({"album_art": ""})

        # Default return for unknown sources
        return jsonify({"album_art": ""})

    except Exception as e:
        logger.error(
            f"Error fetching album art for {source}/{media_type}/{item_id}: {e}"
        )
        return jsonify({"album_art": ""})


@app.route("/api/browse")
def browse_downloads():
    try:
        files = []
        for root, dirs, filenames in os.walk(DOWNLOAD_DIR):
            for filename in filenames:
                if filename.endswith((".mp3", ".flac", ".m4a", ".opus")):
                    filepath = os.path.join(root, filename)
                    rel_path = os.path.relpath(filepath, DOWNLOAD_DIR)
                    files.append(
                        {
                            "name": rel_path,
                            "size": os.path.getsize(filepath),
                            "modified": os.path.getmtime(filepath),
                        }
                    )

        files.sort(key=lambda x: x["modified"], reverse=True)
        return jsonify(files[:100])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/library")
def library_albums():
    """List the Library's album folders (Artist -> Album) for the Files tab tree.

    Cheap directory walk only — no tags are read here, so it returns instantly
    even on large libraries (the per-track tags are fetched lazily per album by
    /api/library/album). The Library is read-only (ADR-0003): a folder on disk
    carries no source URL, so there is no Redownload here."""
    try:
        return jsonify({"albums": list_library_albums(DOWNLOAD_DIR)})
    except Exception as e:
        logger.exception("Failed to list Library albums")
        return jsonify({"error": str(e)}), 500


@app.route("/api/library/album")
def library_album_tracks():
    """Lazily read one album folder's present tracks, with titles and track
    numbers from embedded tags (ADR-0003). Called when the user expands an album
    in the tree.

    The ``path`` query parameter is the album's path relative to DOWNLOAD_DIR, as
    returned by /api/library; it is confined to DOWNLOAD_DIR so the endpoint
    cannot be used to read tags from arbitrary places on disk."""
    rel_path = request.args.get("path")
    if not rel_path:
        return jsonify({"error": "path is required"}), 400

    # Confine the resolved album folder to DOWNLOAD_DIR (reject traversal).
    base = os.path.realpath(DOWNLOAD_DIR)
    target = os.path.realpath(os.path.join(base, rel_path))
    if target != base and not target.startswith(base + os.sep):
        return jsonify({"error": "invalid path"}), 400
    if not os.path.isdir(target):
        return jsonify({"error": "album not found"}), 404

    try:
        payload = get_album_assessment(DOWNLOAD_DIR, rel_path)
        return jsonify(
            {
                "path": rel_path,
                "tracks": payload["tracks"],
                "completeness": payload["completeness"],
            }
        )
    except Exception as e:
        logger.exception(f"Failed to read album tracks for {rel_path}")
        return jsonify({"error": str(e)}), 500


def get_qobuz_credentials():
    try:
        if os.path.exists(STREAMRIP_CONFIG):
            with open(STREAMRIP_CONFIG, "r") as f:
                config_content = f.read()

            app_id = re.search(r'app_id\s*=\s*["\']?([^"\'\n]+)["\']?', config_content)
            token = re.search(r'password_or_token\s*=\s*"([^"]+)"', config_content)

            return {
                "app_id": app_id.group(1).strip() if app_id else "950096963",
                "token": token.group(1).strip() if token else None,
            }
    except Exception as e:
        logger.error(f"Error reading Qobuz credentials: {e}")
    return {"app_id": "950096963", "token": None}


def fetch_single_album_art(item_id, media_type, app_id):
    creds = get_qobuz_credentials()
    if not creds["token"]:
        return {}

    try:
        response = requests.get(
            f"https://www.qobuz.com/api.json/0.2/{media_type}/get",
            params={
                "app_id": creds["app_id"],
                f"{media_type}_id": item_id,
            },
            headers={
                "X-App-Id": creds["app_id"],
                "X-User-Auth-Token": creds["token"],
            },
            timeout=3,
        )
        if response.status_code == 200:
            data = response.json()
            image = data.get("image", {})

            year = None
            release_date = data.get("release_date_original", "")
            if release_date:
                year = release_date[:4]

            return {
                "album_art": image.get("large")
                or image.get("small")
                or image.get("thumbnail")
                or "",
                "tracks_count": data.get("tracks_count"),
                "release_type": data.get("release_type"),
                "year": year,
            }
    except Exception as e:
        logger.error(f"Error fetching Qobuz album art: {e}")
    return {}


def get_qobuz_app_id():
    try:
        if os.path.exists(STREAMRIP_CONFIG):
            with open(STREAMRIP_CONFIG, "r") as f:
                config_content = f.read()
                # logger.debug(f"Config file content: {config_content[:200]}...")  # First 200 chars

            app_id_match = re.search(
                r'app_id\s*=\s*["\']?([^"\'\n]+)["\']?', config_content
            )

            if app_id_match:
                app_id = app_id_match.group(1).strip()
                logger.debug(f"Found app_id in config: {app_id}")
                return app_id
            else:
                logger.debug("No app_id found in config, using fallback")

        # Return a known working app_id as fallback
        fallback_app_id = "950096963"
        logger.debug(f"Using fallback app_id: {fallback_app_id}")
        return fallback_app_id

    except Exception as e:
        logger.error(f"Error extracting app_id: {e}")
        return "950096963"


def construct_url(source, media_type, item_id):
    if not item_id:
        return ""

    url_patterns = {
        "qobuz": {
            "album": f"https://open.qobuz.com/album/{item_id}",
            "track": f"https://open.qobuz.com/track/{item_id}",
            "artist": f"https://open.qobuz.com/artist/{item_id}",
            "playlist": f"https://open.qobuz.com/playlist/{item_id}",
        },
        "tidal": {
            "album": f"https://tidal.com/browse/album/{item_id}",
            "track": f"https://tidal.com/browse/track/{item_id}",
            "artist": f"https://tidal.com/browse/artist/{item_id}",
            "playlist": f"https://tidal.com/browse/playlist/{item_id}",
        },
        "deezer": {
            "album": f"https://www.deezer.com/album/{item_id}",
            "track": f"https://www.deezer.com/track/{item_id}",
            "artist": f"https://www.deezer.com/artist/{item_id}",
            "playlist": f"https://www.deezer.com/playlist/{item_id}",
        },
        "soundcloud": {
            "track": f"https://soundcloud.com/{item_id}",
            "album": f"https://soundcloud.com/{item_id}",
            "playlist": f"https://soundcloud.com/{item_id}",
        },
    }

    if source in url_patterns and media_type in url_patterns[source]:
        return url_patterns[source][media_type]

    return f"https://open.{source}.com/{media_type}/{item_id}"


def extract_metadata_from_url(url):
    metadata = {
        "service": None,
        "type": None,
        "id": None,
        "title": None,
        "artist": None,
        "album_art": None,
    }

    try:
        if "spotify.com" in url:
            metadata["service"] = "spotify"
            match = re.search(r"/(album|track|playlist|artist)/([a-zA-Z0-9]+)", url)
            if match:
                metadata["type"] = match.group(1)
                metadata["id"] = match.group(2)
                # Note: Spotify requires OAuth for metadata, so we can't easily fetch it

        elif "qobuz.com" in url:
            metadata["service"] = "qobuz"
            match = re.search(r"/(album|track|playlist|artist)/([0-9]+)", url)
            if match:
                metadata["type"] = match.group(1)
                metadata["id"] = match.group(2)
                metadata.update(fetch_qobuz_metadata(metadata["id"], metadata["type"]))

        elif "tidal.com" in url:
            metadata["service"] = "tidal"
            match = re.search(r"/(album|track|playlist|artist)/([0-9]+)", url)
            if match:
                metadata["type"] = match.group(1)
                metadata["id"] = match.group(2)
                metadata["album_art"] = (
                    f"https://resources.tidal.com/images/{metadata['id']}/320x320.jpg"
                )

        elif "deezer.com" in url:
            metadata["service"] = "deezer"
            match = re.search(r"/(album|track|playlist|artist)/([0-9]+)", url)
            if match:
                metadata["type"] = match.group(1)
                metadata["id"] = match.group(2)
                metadata.update(fetch_deezer_metadata(metadata["id"], metadata["type"]))

    except Exception as e:
        logger.error(f"Error extracting metadata from URL: {e}")

    return metadata


def fetch_qobuz_metadata(item_id, item_type):
    metadata = {}
    try:
        app_id = get_qobuz_app_id()
        api_base = "https://www.qobuz.com/api.json/0.2"

        if item_type == "album":
            response = requests.get(
                f"{api_base}/album/get",
                params={"album_id": item_id, "app_id": app_id},
                timeout=5,
            )
            if response.status_code == 200:
                data = response.json()
                metadata["title"] = data.get("title", "")
                metadata["artist"] = data.get("artist", {}).get("name", "")
                if "image" in data:
                    for size in ["small", "medium", "large", "thumbnail"]:
                        if size in data["image"]:
                            metadata["album_art"] = data["image"][size]
                            break

        elif item_type == "track":
            response = requests.get(
                f"{api_base}/track/get",
                params={"track_id": item_id, "app_id": app_id},
                timeout=5,
            )
            if response.status_code == 200:
                data = response.json()
                metadata["title"] = data.get("title", "")
                metadata["artist"] = data.get("performer", {}).get("name", "")
                album = data.get("album", {})
                if "image" in album:
                    for size in ["small", "medium", "large", "thumbnail"]:
                        if size in album["image"]:
                            metadata["album_art"] = album["image"][size]
                            break

    except Exception as e:
        logger.error(f"Error fetching Qobuz metadata: {e}")

    return metadata


def fetch_deezer_metadata(item_id, item_type):
    metadata = {}
    try:
        api_base = "https://api.deezer.com"

        if item_type == "album":
            response = requests.get(f"{api_base}/album/{item_id}", timeout=5)
            if response.status_code == 200:
                data = response.json()
                metadata["title"] = data.get("title", "")
                metadata["artist"] = data.get("artist", {}).get("name", "")
                metadata["album_art"] = data.get("cover_medium", "")

        elif item_type == "track":
            response = requests.get(f"{api_base}/track/{item_id}", timeout=5)
            if response.status_code == 200:
                data = response.json()
                metadata["title"] = data.get("title", "")
                metadata["artist"] = data.get("artist", {}).get("name", "")
                album = data.get("album", {})
                metadata["album_art"] = album.get("cover_medium", "")

    except Exception as e:
        logger.error(f"Error fetching Deezer metadata: {e}")

    return metadata


@app.route("/api/download-from-url", methods=["POST"])
def download_from_url():
    data = request.json
    url = data.get("url")
    quality = data.get("quality", 3)

    title = data.get("title")
    artist = data.get("artist")
    album_art = data.get("album_art")
    service = data.get("service")

    if not url:
        return jsonify({"error": "URL required"}), 400

    if title and artist and service:
        metadata = {
            "title": title,
            "artist": artist,
            "album_art": album_art,
            "service": service,
        }
    else:
        metadata = extract_metadata_from_url(url)

    task_id = enqueue_download(url, quality, metadata)

    return jsonify({"task_id": task_id, "status": "queued", "metadata": metadata})


@app.route("/api/redownload", methods=["POST"])
def redownload():
    """Redownload a History entry, bypassing the Streamrip database.

    Re-enqueues the item identified by its History ``id`` as a brand-new
    Download, reusing the original URL, quality, and metadata, with --no-db so
    streamrip ignores its own record and downloads it again even when it would
    otherwise Skip (see the Redownload glossary entry). The new Download follows
    the normal lifecycle: it is registered as Queued and visible immediately,
    with a fresh id distinct from the original History entry."""
    data = request.json or {}
    entry_id = data.get("id")

    if not entry_id:
        return jsonify({"error": "History entry id is required"}), 400

    with history_lock:
        entry = next((e for e in download_history if e["id"] == entry_id), None)

    if entry is None:
        return jsonify({"error": "History entry not found"}), 404

    url = entry.get("url")
    if not url:
        # Redownload is only meaningful for items whose source URL is known.
        return jsonify({"error": "History entry has no source URL to redownload"}), 400

    quality = entry.get("quality")
    if quality is None:
        quality = 3
    metadata = entry.get("metadata") or {}

    task_id = enqueue_download(url, quality, metadata, no_db=True)

    return jsonify(
        {
            "task_id": task_id,
            "status": "queued",
            "metadata": metadata,
        }
    )


if __name__ == "__main__":
    logger.info("Starting Streamrip Web application...")
    logger.info(f"Config path: {STREAMRIP_CONFIG}")
    logger.info(f"Download directory: {DOWNLOAD_DIR}")
    logger.info(f"Max concurrent downloads: {MAX_CONCURRENT_DOWNLOADS}")
    app.run(host="0.0.0.0", port=5000, debug=False)
