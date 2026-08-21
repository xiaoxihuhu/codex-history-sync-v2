# Phase 1 Baseline Audit

Date: 2026-08-21

This document records the V1 baseline and the local Codex format observations
that are safe to keep in the repository. It intentionally omits conversation
content, account identifiers, credentials, tokens, and machine-specific
absolute paths.

## Repository Guard

- `origin` points to `xiaoxihuhu/codex-history-sync-v2`.
- No `upstream` remote is configured.
- The V1 baseline was checked out from `origin/main`.
- `v1-stable` points to baseline commit `7447df0`.
- V2 work is on `v2-development`, which tracks the same branch on `origin`.
- The original author's repository was not configured as a write target.

## V1 Baseline

Baseline command:

```text
py -3 -m unittest discover -s tests -v
```

Result on 2026-08-21:

```text
Ran 11 tests in 0.458s
OK
```

The existing implementation is a single Python backend plus a PowerShell
Windows UI. The backend currently provides:

- Codex home detection.
- Root and modern state database detection.
- Database and WAL activity comparison.
- Provider and model status inspection.
- Provider and model repair.
- Session JSONL first-line `session_meta` repair.
- `session_index.jsonl` rebuilding.
- SQLite lock retry and WAL checkpointing.
- Atomic text replacement.
- Local backup and restore.
- Manual provider/model overrides.

The V1 tests remain in place and are the compatibility gate for subsequent
refactoring.

## Local Codex Storage Findings

The audit was performed against the default Codex home using read-only,
metadata-focused probes.

### State database

- The active state database in this environment is the root
  `state_5.sqlite`.
- Its WAL and shared-memory sidecars are present.
- The `sqlite` directory contains `codex-dev.db` and
  `codex-thread-summaries-dev.db`, not a second `state_5.sqlite`.
- The state database has a `threads` table with modern metadata including
  `rollout_path`, `created_at`, `updated_at`, `model_provider`, `cwd`,
  `title`, `archived`, `model`, project fields, and recency fields.
- SQLite `integrity_check` returned `ok`.

Observed relationship counts in the audited local snapshot:

- 33 thread rows.
- 27 active threads and 6 archived threads.
- 32 session index entries.
- 27 active session JSONL files.
- All 33 database `rollout_path` values resolve to existing files.
- Active rollouts are under `sessions`; archived rollouts are under
  `archived_sessions`.

The counts are an observation of one local snapshot, not a product invariant.
The implementation must treat the database, session files, and index as
independent sources that require verification.

### Session JSONL

All sampled session files begin with a `session_meta` record. Its payload
contains fields such as:

```text
id, session_id, model_provider, cwd, source, thread_source,
history_mode, cli_version, context_window, git, dynamic_tools,
parent_thread_id, originator
```

The sampled `session_meta` records do not contain a `model` field. Model
information appears in `turn_context.model` records instead. Response and
event records include stable identifiers such as turn IDs, call IDs, and
message-related fields that can be used as probe locations, but the exact
attachment-to-message contract is not encoded in the root attachment manifest.

The current V1 scanner correctly parses the sampled active session files, but
it only searches the active `sessions` tree. Archived rollout handling must be
an explicit adapter policy in V2.

### Attachments and images

The audited Codex home contains an `attachments` directory with UUID-named
subdirectories. The sample contains pasted-text files only; no local PNG,
JPEG, WEBP, GIF, PDF, XLSX, DOCX, PPTX, or ZIP entity was found in that
directory. Therefore the tool must not assume that the sample represents all
attachment types.

The root attachment manifest has this shape:

```text
attachmentPaths
pendingRemovalPaths
textExcerptsByPath
```

The manifest tracks local paths and text previews. It does not provide a
thread ID or message ID. Attachment UUIDs are referenced from session JSONL
content, while image inputs were observed as:

- `response_item.content[].type = input_image`
- `response_item.content[].image_url`
- `event_msg.local_images[]`

The local image references can be absolute Windows paths. This is why V2
attachment identity must be based on content hash plus a stable attachment
record, never on the old absolute path.

### Security boundary

The audit confirms that `.codex` contains credential-bearing and
machine-private files. V2 must use a whitelist extractor and must never
upload the entire Codex home. In particular, authentication files,
global state, provider secrets, and raw credentials are outside the sync
manifest.

## V2 Adapter Decisions

1. Keep `state_5.sqlite` detection separate from auxiliary Codex databases.
2. Make the local adapter aware of active and archived rollout trees.
3. Parse thread metadata from SQLite and session metadata from JSONL without
   assuming that `session_meta` carries the current model.
4. Probe attachment references from both session content and event-level
   local image fields.
5. Normalize absolute paths into logical references before cloud upload.
6. Require SHA256 and existence checks for every extracted attachment.
7. Preserve raw reference locations for later message/session repair.
8. Keep V1 repair, backup, restore, and PowerShell UI behavior unchanged
   during the first refactoring phase.

## Known Unknowns

- The audited home does not contain representative binary image/document
  entities, so binary attachment placement needs a fixture or a later real
  sample.
- The root attachment manifest does not define an explicit message relation.
- Generated image references and user-uploaded image references need to be
  distinguished by additional fixtures.
- Codex schema/version detection should be adapter-based instead of relying
  on one hard-coded set of fields.

Phase 2 should extract the Local Repair Engine behind compatibility-preserving
interfaces. Phase 3 should add the attachment probe and anonymous fixtures
before implementing cloud upload.
