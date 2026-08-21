# Phase 7: Pure-Text Cloud Restore

Phase 7 restores cloud Thread metadata and Session JSONL to an initialized Codex home.
It supports all Threads or one specified Codex Thread ID.

## Prerequisites

The target computer must have:

- Codex Desktop initialized once;
- a valid `.codex/config.toml`;
- a Codex state database containing a `threads` table;
- the V2 Supabase migrations and private Storage bucket;
- a signed-in Codex Sync account.

The restore command does not create or replace Codex's internal database schema.

## Commands

Restore all missing pure-text history:

```powershell
py -3 .\sync_backend.py --json cloud-restore
```

Restore one Thread:

```powershell
py -3 .\sync_backend.py --json cloud-restore --thread-id THREAD_UUID
```

Assign restored Threads to an existing local working directory:

```powershell
py -3 .\sync_backend.py --json cloud-restore --target-cwd E:\Work\Recovered
```

Without `--target-cwd`, the current Windows user home is used. This is a Phase 7
single-path assignment, not the multi-device Workspace Mapping implemented in Phase 10.

## Restore Pipeline

The command performs these steps:

1. Restore the encrypted Supabase Auth session and register the current device.
2. Read the user's cloud Thread and Session manifests through RLS.
3. Select the Session matching each Thread's `rollout_relative_path`.
4. Reject absolute, parent-traversal, non-canonical, or cross-user Storage paths.
5. Skip Storage downloads when the target Session already exists locally.
6. Download only missing JSONL objects to a temporary staging directory.
7. Verify file size, SHA256, UTF-8 JSONL, first-line `session_meta`, and Thread ID.
8. Create a V1-compatible safety backup before the first local mutation.
9. Atomically create missing Session files.
10. Insert only missing Thread rows through a target-Schema adapter.
11. Apply the target computer's Provider, Model, and local cwd to restored metadata.
12. Run Local Repair Engine primitives and rebuild `session_index.jsonl`.
13. Verify SQLite integrity, Thread rows, Session files, Session IDs, and active index entries.

## Compatibility Adapter

The local adapter reads `PRAGMA table_info(threads)` and writes only columns present on
the target computer. It supports:

- legacy tables containing only `id`, `model_provider`, and optional `model`;
- modern tables containing `rollout_path`, timestamps, cwd, title, archive state,
  sandbox/approval metadata, and newer optional columns.

Unknown required columns without defaults cause a pre-write failure. For modern empty
databases, the adapter uses a read-only managed sandbox policy and `on-request`
approval mode. If the target database already has Threads, its latest compatibility
values are reused.

Windows extended paths using the `\\?\` prefix compare correctly with normal local
paths.

## Rollback

The restore is missing-only and never overwrites a different existing Session file.

If any database, repair, index, or verification step fails after mutation starts, the
client:

- restores the pre-restore SQLite backup;
- restores the previous Session first-line metadata;
- removes Session files created by the failed run;
- restores the previous index or removes a newly created index.

The safety backup is retained for audit and manual recovery.

## Phase Boundary

Phase 7 restores plain-text history only. It does not yet restore images, documents,
attachment references, per-Workspace mappings, automatic queues, or Snapshots.
