# Connected Supabase Compatibility Audit

Date: 2026-08-21

This was a read-only audit of the connected healthy Supabase project named
`codex-sync`. No migration, branch, table, policy, bucket, Auth configuration,
or row was changed.

## Existing Project

The project already has a migration named `init_codex_sync` and an earlier
prototype schema:

```text
profiles
devices
projects
project_devices
sessions
session_versions
sync_objects
sync_events
conflicts
user_settings
```

The old schema and the Phase 4 V2 schema have overlapping table names but
incompatible keys and columns. Examples:

- Old `profiles` uses `user_id`; V2 uses `id`.
- Old `devices` uses `(user_id, device_id text)`; V2 uses a UUID `id` plus a
  composite ownership key.
- Old `sessions` uses `(user_id, session_id text)`; V2 uses a UUID row ID and a
  foreign key to the V2 Thread entity.
- Old `sync_events` uses `event_id` and `operation`; V2 uses `id`, `run_id`,
  direction, entity type, retry count, and structured details.
- The old project has no V2 Thread, Attachment, Attachment Reference,
  Workspace, Snapshot, or Snapshot Item table.

Therefore the fresh-install Phase 4 migrations must not be applied directly to
the existing main project.

## Existing Security

- Existing public tables have owner RLS policies for authenticated users.
- Storage has owner policies for select, insert, update, and delete.
- The existing private bucket is named `codex-sync`.
- Existing Storage object ownership uses the first path segment as the user ID.
- The old public tables still show broad grants for both `anon` and
  `authenticated`. `anon` currently has no matching row policies, but the V2
  upgrade should explicitly revoke unnecessary `anon` privileges.

The security advisor reported one Auth warning: leaked-password protection is
disabled. That is a project setting and is not changed by repository SQL.

The performance advisor reported:

- A missing covering index on the old `project_devices` device foreign key.
- A missing `user_id` index on the old `sync_events`.
- One unused old conflict-status index.

## Migration Decision

1. Keep the existing main project unchanged.
2. Implement and test the desktop Auth/Devices client against an abstract V2
   repository contract.
3. Create a dedicated compatibility migration after confirming whether the
   old prototype tables contain data.
4. Test that migration on a Supabase development branch before any main
   project DDL.
5. Preserve or transform existing rows if data exists; never drop the old
   schema blindly.
6. Re-run security and performance advisors after the branch migration.

Creating a development branch can incur Supabase cost and was not requested,
so no branch was created during this audit.
