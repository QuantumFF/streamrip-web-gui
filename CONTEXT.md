# Context: Streamrip Web GUI

A web front-end over the `streamrip` CLI (`rip`) for searching music sources and
downloading albums, tracks, and playlists.

## Glossary

### Download
A single user-requested unit of work, corresponding to one `rip url <url>`
invocation. A Download is *not* a single audio file — an album Download produces
many track files. A Download moves through a fixed lifecycle (see **Download
lifecycle**).

### Download lifecycle
The states a Download passes through, in order:

- **Queued** — accepted by the server and waiting for a free worker. A Queued
  Download is visible to the user the instant it is submitted, even when no
  worker is free to start it.
- **Downloading** — a worker is actively running `rip` for this Download.
- **Completed** — `rip` finished and fetched the tracks successfully.
- **Failed** — `rip` exited with an error.
- **Skipped** — `rip` did no work because every track was already recorded in
  the streamrip database (see **Streamrip database**). Surfaced to the user as
  "already downloaded".

A Download is *active* while it is Queued or Downloading. Once it reaches a
terminal state (Completed / Failed / Skipped) it becomes part of the **History**.

### Active
The set of Downloads that are currently Queued or Downloading. Presented to the
user as a single unified list, not split by state.

### History
The record of Downloads that have reached a terminal state.

### Streamrip database
The SQLite database maintained by `rip` itself, recording what it has already
downloaded so it can skip re-downloading. This is distinct from any state this
web app keeps. Bypassing it (so an already-recorded item downloads again) is a
**Redownload**.

### Redownload
Re-running a Download for an item that streamrip would otherwise Skip, forcing
it to ignore the **Streamrip database**. Only meaningful for items whose source
URL is known — i.e. items in the **History** — never for arbitrary folders on
disk.

### Library
The set of already-downloaded albums as they exist *on disk*, independent of
this app's Download History. The Library is the unit the user browses to verify
**Album completeness**.

### Album completeness
Whether every track an album is supposed to contain is actually present on disk.
The expected track count is not stored by this app — it is read from the
embedded tags (`tracktotal`, `disctotal`, `tracknumber`, `discnumber`) of the
tracks that *are* present, which streamrip writes into every file. An album is:

- **Complete** — every expected (disc, track) is present on disk.
- **Incomplete** — at least one expected track is absent. An absent track whose
  position is determinable is a **Missing track**, identified by its number (its
  title is unknown, because a track that was never downloaded left no tags on
  disk). On a multi-disc album the tags carry only the album-wide total, so a
  trailing absence there is counted exactly but its disc/position is unknown
  (an **unlocated** absence); a disc with no tracks on disk at all is a
  **Missing disc**.
- **Unknown** — completeness cannot be determined, because no present track has
  readable tags to reveal the expected total.

### Source
A single music service (Qobuz, Tidal, Deezer, SoundCloud, ...) and everything
that is specific to it: how to build a share URL from an id and read an id back
out of one (**url** / parse, both directions), how to fetch its **album art**,
and how to read **metadata** from one of its URLs. A Source owns its
credentials/keys too (Qobuz, for instance, reads its app_id and auth token from
the **Streamrip database**'s config). Each Source reaches the network only
through the injectable `http_get` seam, mirroring the `run_rip` and
`read_audio_tags` seams, so an adapter can be exercised with canned JSON and no
real network. Sources are migrated behind this single interface one at a time;
a not-yet-migrated source keeps its quirks inline (e.g. in `construct_url` and
`extract_metadata_from_url`) until its slice lands.
