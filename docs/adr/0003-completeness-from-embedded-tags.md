# Album completeness is derived from embedded tags, not a stored tracklist

The Library/Files view verifies whether an album on disk is complete by reading
the embedded tags (`tracktotal`, `disctotal`, `tracknumber`, `discnumber`) of
the tracks that are present, and comparing the present track numbers against the
expected `1…tracktotal` per disc. streamrip writes these tags into every file
(`metadata/tagger.py`), and mutagen — already a streamrip dependency — reads
them back.

## Considered Options

- **Store the expected tracklist per download** (or re-fetch it from the source
  on demand). Requires persisting metadata keyed to each folder, or a network
  round-trip and the source URL/ID — which a bare folder on disk does not carry.
- **Infer gaps from track-number sequence alone.** Cannot detect trailing gaps
  (e.g. tracks 10–12 missing) because the true total is unknown.
- **Read totals from present tags (chosen).** Any single present track reveals
  the album's true `tracktotal`, so all gaps — including trailing ones — are
  detectable from disk alone, with no network, no stored metadata, and no DB.

## Tag semantics (verified empirically against real rips)

streamrip writes `tracktotal` as the **album-wide** track count while
`tracknumber` **restarts per disc**; there is no per-disc total in the tags.
`disctotal` reveals how many discs the album has. Multi-disc audio sits in
`Disc N` subfolders of a flat album folder (the default `folder_format` has no
artist directory level).

## Consequences

A missing track can only be shown by number ("Track 7 — missing"), never by
title, because a track that was never downloaded left no tags on disk. An album
with zero readable-tag tracks is reported as **Unknown** rather than guessed.
On a multi-disc album the *number* of absent tracks is exact (album total minus
present count) but a trailing gap cannot be attributed to a specific disc —
those are reported as "position unknown" rather than guessed; an entirely
absent disc is detected via `disctotal` and reported as a missing disc.
The Library view is read-only: it cannot offer Redownload, because a folder on
disk carries no source URL (Redownload lives in History — see the Redownload
glossary entry).
