# Supabase Schema

Phase 4 defines the cloud data model and security policies. It does not contain
a project URL, API key, Auth session, or service-role credential.

## Migration Order

Apply the SQL files in lexical order:

```text
001_profiles.sql
002_devices.sql
003_workspaces.sql
004_threads.sql
005_sessions.sql
006_attachments.sql
007_sync_events.sql
008_snapshots.sql
009_rls.sql
010_native_state.sql
011_native_state_fk_fix.sql
```

The numbered files are repository source migrations. When a Supabase project
is connected, they can be applied to an isolated development branch and then
recorded using that environment's current Supabase CLI migration workflow.

## Ownership Model

- `profiles.id` is the Supabase Auth user ID.
- A restricted trigger on `auth.users` creates the matching Profile after
  signup. The trigger function lives in the non-exposed private schema, fixes
  an empty `search_path`, and is not executable by client roles.
- Every other client-visible row carries `user_id`.
- Cross-table relationships use composite `(user_id, id)` foreign keys where
  applicable. A client cannot create an owned child row that points to another
  user's parent row.
- RLS uses `(select auth.uid())` and never uses email, user-editable metadata,
  or `auth.role()` for authorization.
- `anon` receives no table privileges.
- `authenticated` receives explicit Data API privileges, and RLS limits those
  privileges to the current user's rows.

## Storage

The private bucket is:

```text
codex-history-sync
```

Object names must begin with:

```text
users/USER_ID/
```

Storage policies cover `SELECT`, `INSERT`, `UPDATE`, and `DELETE`. The
`SELECT + INSERT + UPDATE` combination is required for client-side upsert.

Recommended object paths:

```text
users/USER_ID/sessions/SHA256.jsonl
users/USER_ID/attachments/HASH_PREFIX/SHA256.ext
users/USER_ID/snapshots/SNAPSHOT_ID/manifest.json
```

The database constraints enforce the user prefix for Session, Attachment, and
Snapshot manifest paths.

## Attachment Deduplication

`attachments` stores one content entity per `(user_id, sha256)`.
`attachment_references` stores the Thread, Session, Message, original path, and
JSONL reference locations that point to that content. This supports
content-addressed upload without losing multiple message relationships.

## Validation

The repository test suite validates the migration security contract:

```powershell
py -3 -m unittest tests.test_migrations -v
py -3 -m unittest discover -s tests -v
```

After a Supabase project and development branch are available, Phase 4 still
requires live verification:

1. Apply all migrations to the development branch.
2. Run security and performance advisors.
3. Test two distinct Auth users against every table and Storage operation.
4. Confirm cross-user reads, writes, updates, deletes, and object upserts fail.
5. Confirm no table is exposed to `anon`.

Live application is intentionally deferred until a project is connected; the
repository contains no hidden project identifier or credential.
