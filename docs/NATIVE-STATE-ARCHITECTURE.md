# Native State Architecture

Phase 10 adds a cloud-safe Native State design beside the existing V2 Core
backup flow. It does not upload data or change the formal GUI restore path.

## Boundaries

- **Cloud Core** remains responsible for the existing Thread, Session,
  Attachment, and Snapshot flow.
- **Native State** is a versioned metadata export for Codex-native SQLite
  state.
- **Session** and **Attachment** remain separate payloads. Native State does
  not contain Raw SQLite or Session JSONL bytes.
- **Native Snapshot** means a local SQLite Backup API safety snapshot. It is
  not a Raw Cloud SQLite snapshot.

The intended future restore composition is:

```text
Cloud Core + Native State + Session + Attachment
```

Phase 10 stops at a local codec and isolated repository contract. Phase 11 is
where a normal authenticated Cloud client may be connected.

## Source And Target Contracts

The source SQLite schema is inspected dynamically. A Native Export preserves
source metadata in `native_metadata` and records the source schema fingerprint.
It does not assume that all Codex versions have the same columns.

The target database remains the target contract. A future merge uses
`PathMappingSet`, project ID remapping, target-column availability, and
non-destructive merge rules. Existing target Threads and Projects are
preserved.

## Project Identity And Path Mapping

`native_projects` and `native_project_roots` store source identity and source
paths. A source Project ID is not reused as a target Project ID across
computers because the target database may already contain a different Project
with the same local identity or a different mapped root.

The future restore sequence is:

```text
source_path -> PathMappingSet -> target_path
source_project_id -> ProjectIdRemap -> target_project_id
```

The target path must never be written back into the source root fields.
Workspace Path Mapping is a target-device concern, not a replacement for
Codex Project metadata.

## Safety Policy

Only these SQLite tables are eligible for Native State encoding:

- `threads`
- `projects`
- `project_roots`
- `thread_sections`
- `thread_dynamic_tools`
- `thread_spawn_edges`

Unknown tables are denied by default. Metadata fields containing tokens,
secrets, passwords, credentials, cookies, API keys, authorization values, or
session tokens are removed recursively. `model_provider` is explicitly not
treated as a secret field.

The source `rollout_path` is kept inside source metadata for source truth, but
the cloud query field uses a relative `sessions/...` representation. Target
absolute paths are produced only during a future local merge.

## Versioning And Hashes

`NativeStateExport.format_version` is currently `1`. Unknown versions fail
with `UnsupportedNativeStateFormatError`; they are never silently decoded.

`metadata_hash` is:

```text
SHA256(canonicalize_native_metadata(native_metadata).encode("utf-8"))
```

Canonicalization sorts keys, emits deterministic UTF-8 JSON, normalizes
supported values, and excludes transient local-only fields such as database
and machine identity.

The schema fingerprint is a SHA256 of a canonical representation containing
table names, column definitions, foreign keys, and indexes. It excludes
absolute paths, usernames, and machine names.

## Snapshot Semantics

Every completed Native State upload is a new immutable logical export:

```text
Backup Run #1 -> Export A
Backup Run #2 -> Export B
Backup Run #3 -> Export C
```

Sub-rows are written while `is_complete = false`. A restore may select only a
completed export. The additive `snapshots.native_export_id` field can point an
existing Snapshot at a Native Export while old Snapshots with `NULL` continue
to work.

## Rollback

`migrations/010_native_state.sql` is forward-only and additive. A rollback
must remove only the five Native tables, their indexes and policies, and the
nullable `snapshots.native_export_id` column after any dependent Native data
has been intentionally removed. It must not drop or rewrite:

```text
profiles, devices, workspaces, threads, sessions, attachments,
attachment_references, sync_events, snapshots, snapshot_items
```

Phase 10 has no production deployment. The migration is a reviewed SQL
contract only.
