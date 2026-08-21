# V2 Operations

## Incremental state

The standalone `%LOCALAPPDATA%\CodexHistorySync\sync_state.sqlite` stores
Workspace mappings, object Manifest metadata, and upload queue state. Codex's
own SQLite files are not used as a V2 control database.

For each Session object, the local state records:

- object kind and stable object key
- file size and modification time
- current SHA256
- last cloud-verified SHA256 and timestamp

The cloud Hash remains authoritative for deciding whether a Storage object can
be reused. Local state is updated only after the cloud Session manifest has
been re-read and verified.

## Retry queue

An object is placed in `pending` state before upload work starts. A network or
verification error stores a bounded error summary and exponential retry time.
The queue is safe across application restarts. Authentication tokens,
passwords, and API keys are never queue fields.

## Snapshots

`cloud-snapshot-create` captures the current cloud Thread, Session, Attachment,
and reference manifests into a content-addressed private Storage object. The
database row stays `pending` until the Storage object and Snapshot Items are
written. Any failure marks the row `failed`.

`cloud-snapshot-restore` validates the selected Manifest SHA256 and uses only
the Thread and Session rows inside that Manifest. It then delegates local
database writes, Workspace resolution, backups, rollback, and verification to
the existing restore engine.

## Build prerequisites

The core CLI has no third-party runtime dependency. GUI and EXE builds are
optional:

```powershell
python -m pip install .[gui,build]
```

The Windows build script checks Python, runs the complete test suite, verifies
the optional imports, invokes PyInstaller, and checks that
`dist\CodexHistorySync.exe` exists. It refuses output paths outside the
repository.
