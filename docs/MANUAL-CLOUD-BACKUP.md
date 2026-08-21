# Phase 6: Manual Thread and Session Backup

Phase 6 adds an explicit one-way backup command:

```text
local Codex home -> Supabase PostgreSQL + private Storage
```

It does not upload the complete `.codex` directory or `state_5.sqlite`.

## Command

After applying the V2 migrations, configuring a client-safe Supabase key, and signing in:

```powershell
py -3 .\sync_backend.py --json cloud-backup
```

Use `--codex-home` before the command to test an isolated Codex home:

```powershell
py -3 .\sync_backend.py --codex-home C:\TestComputerA\.codex --json cloud-backup
```

## Uploaded Data

The scanner reads the selected Codex state database in read-only mode and uploads only:

- whitelisted Thread metadata from the `threads` table;
- Session JSONL files referenced by `rollout_path`;
- relative paths below `sessions/` or `archived_sessions/`;
- file size, source mtime, SHA256, Session ID, and Codex timestamps.

Absolute Session paths are never stored in the cloud. A Session outside the two allowed
Codex rollout trees is rejected.

## Incremental Behavior

Session files are read with a stable-file check. Size and mtime must remain unchanged
before and after the read.

The content SHA256 is compared with the existing cloud Session row:

- unchanged path and hash: no Storage upload and no Session upsert;
- changed hash: upload the new content-addressed object and upsert the Session row;
- duplicate hash in the same account: reuse the existing Storage object path.

Storage objects use:

```text
users/USER_ID/sessions/SHA256.jsonl
```

Thread metadata is upserted on every manual run so title, archive state, provider, model,
and timestamps remain current.

## Completion Verification

After all changed Session rows are written, the client reads the cloud Session manifest
again and verifies every local `(thread_id, relative_path)` against the expected SHA256.

Only after that verification succeeds does the client:

- return a successful `cloud-backup` result;
- update the local device `last_backup_at`;
- upsert the completed backup timestamp to the cloud device record.

HTTP success alone is not treated as backup success.

## Current Deployment Constraint

The connected `codex-sync` Supabase project still contains the earlier prototype schema.
Its `threads`, `sessions`, `devices`, and `profiles` contracts are incompatible with the
fresh V2 migrations in this repository.

Do not run `cloud-backup` against that prototype schema. Apply a reviewed compatibility
migration or deploy migrations `001` through `009` to a clean V2 project first. The
desktop client requires only a publishable/anon key plus the signed-in user's Auth
session; a service-role or secret key must never be configured.

## Phase Boundary

This phase intentionally excludes Workspace metadata, attachments, automatic scheduling,
persistent queues, downloads, restores, and Snapshots. Those are implemented in later
phases.
