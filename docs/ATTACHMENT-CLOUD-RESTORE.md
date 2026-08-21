# Phase 9: Attachment Cloud Restore

Run pure-text restore first, then restore attachments:

```powershell
py -3 .\sync_backend.py --json cloud-restore
py -3 .\sync_backend.py --json cloud-restore-attachments
```

The client downloads only missing attachment objects to a temporary directory, verifies
size and SHA256, and stores them under:

```text
.codex/restored_attachments/HASH_PREFIX/SHA256.ext
```

It never recreates the old computer's username or drive path. For references associated
with a restored Session, JSONL is parsed line by line and only recorded string values are
replaced. Embedded `data:` content remains unchanged.

Before mutation the client creates a safety backup and retains exact copies of every
Session it will edit. Attachment files and Session rewrites use atomic replacement.
On failure, original Session bytes are restored and files created by the failed run are
removed. Rollback attempts every item even when one rollback action fails.

Verification checks every restored file's SHA256 and confirms the repaired Session paths
are visible to Attachment Probe. Re-running the command reuses existing verified files
and does not rewrite Sessions that already point to the restored location.

Phase 10 adds persistent Workspace Mapping. Phase 9 repairs attachment paths only.
