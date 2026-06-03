# Download state is held in memory; streamrip's DB is the durability backstop

The server owns the authoritative Active, Queue, and History state in memory and
the frontend rehydrates it from `/api/status` on load. State survives a page
refresh but not a server/container restart. We deliberately did not add a
persistence layer (SQLite/JSON) at this stage.

## Considered Options

- **Persist History/queue to disk.** Survives restarts and gives a permanent,
  queryable log, but adds a schema, migrations, and write-concurrency handling.
- **In-memory, server as source of truth (chosen).** Far less machinery; a page
  refresh is seamless via `/api/status`.

## Consequences

History vanishes on restart, which is acceptable because streamrip's own SQLite
database (see ADR-0003 and the Streamrip database glossary entry) is the real
record of what has been downloaded — nothing is permanently lost. The code is
structured so a disk-persistence layer can be dropped in later without changing
the API surface.
