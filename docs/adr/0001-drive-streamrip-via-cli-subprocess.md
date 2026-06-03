# Drive streamrip via the `rip` CLI subprocess, not as a Python library

We considered importing `streamrip` and driving downloads in-process to obtain
accurate per-track progress via its `get_progress_callback` hook. We chose
instead to keep shelling out to the `rip` CLI with `subprocess`.

## Considered Options

- **Import streamrip as a library.** Would give true per-track byte progress and
  exact track totals, but couples us to streamrip's internal async API (which
  changes between versions), removes process isolation (a streamrip crash would
  take down a Flask worker), and forces asyncio into our threaded worker model.
- **Shell out to `rip` (chosen).** Stable public interface, full process
  isolation, trivial to upgrade streamrip.

## Consequences

We cannot observe fine-grained download progress: `rip` renders per-track byte
bars through `rich.Live` and logs nothing on track success, so progress is not
parseable from stdout. This is why the UI shows a status badge + spinner rather
than a "4/12" track counter. Album completeness is therefore verified after the
fact from disk (see ADR-0003), not streamed live.
