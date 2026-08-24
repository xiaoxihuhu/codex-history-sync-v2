# Native State Cloud Schema

This document describes the additive Phase 10 schema introduced by
`migrations/010_native_state.sql` and corrected by
`migrations/011_native_state_fk_fix.sql`. It is not a deployment instruction.

## Tables

### `native_state_exports`

One immutable logical export run. It records the format version, schema
fingerprint, serialized schema contract, source metadata, row counts, and
completion state. It never stores Raw SQLite bytes.

### `native_projects`

Source Project metadata for one export. `source_project_id` is the identity
from the source SQLite database. The generated UUID is cloud row identity.

### `native_project_roots`

Source Project roots and their source paths. A target computer's mapped path
does not overwrite `source_path`.

### `native_threads`

Core query fields plus `native_metadata`. It stores source Thread identity,
safe display/model fields, a relative rollout path, schema columns, and a
canonical metadata hash. It deliberately avoids a 40-plus-column fixed
mirror of SQLite.

### `native_related_state`

Allowlisted related rows for sections, dynamic tools, and spawn edges. Each
row has an explicit object type, source table, stable object key, safe
metadata, and hash. Other SQLite tables are denied by default.

## Foreign Keys And Deletion

Every Native child row belongs to `native_state_exports` through a
user-scoped foreign key. Deleting an export cascades to its projects, roots,
Threads, and related state. Existing Devices, old V2 Threads, Sessions,
Attachments, and Snapshots are not deleted by that cascade.

`snapshots.native_export_id` is nullable and references an export with
column-scoped `ON DELETE SET NULL (native_export_id)`. Deleting an export
preserves the Snapshot and its `user_id`.

The Device relationships on `native_state_exports` and `native_threads` use
composite `(user_id, source_device_id)` foreign keys. Deleting a Device clears
only `source_device_id`; it never clears the child row's `user_id`.

## RLS And Data API Contract

All five Native tables enable RLS. `anon` has no table grants. The
`authenticated` role receives table privileges, but every policy requires:

```sql
(select auth.uid()) = user_id
```

Child policies additionally verify that their export belongs to the same
authenticated user. Project-root and Thread policies verify their Native
Project relationship when present. This prevents reading or mutating another
user's rows by guessing a UUID.

## Repository Contract

`codex_sync.cloud.native_state.NativeStateRepository` defines:

```text
create_export
upsert_projects
upsert_project_roots
upsert_threads
upsert_related_state
complete_export
list_exports
get_latest_complete_export
get_export
list_projects
list_project_roots
list_threads
list_related_state
delete_export
```

The Phase 10 implementation is
`InMemoryNativeStateRepository`, with
`FakeSupabaseNativeStateRepository` as an explicit fake name for contract
tests. Upload methods accept a batch size, defaulting to 100. They reject
writes after completion and enforce owner/export relationships locally.

`EncryptedNativeSnapshotRepository` is a future-only protocol. No Raw SQLite
upload implementation exists in Phase 10.

## Codec Contract

`codex_sync.native.codec.NativeStateCodec` translates the local
`NativeStateExport` into cloud rows and back. The codec:

- accepts only format version 1;
- computes a stable schema fingerprint;
- uses safe core fields plus `native_metadata`;
- computes `metadata_hash` from canonical UTF-8 JSON;
- keeps source project/root values separate from target mapping;
- maps related tables through an explicit allowlist;
- strips sensitive field names before serialization.

The codec is independent of Supabase HTTP, SQLite mutation, and merge logic.
