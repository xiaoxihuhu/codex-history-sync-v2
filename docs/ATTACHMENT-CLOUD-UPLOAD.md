# Phase 8: Image and Attachment Cloud Upload

Phase 8 uploads attachment entities and their references after Thread and Session backup.
It uses the Phase 3 Attachment Probe instead of assuming one fixed Codex attachment
directory.

## Command

Run the Thread/Session backup first:

```powershell
py -3 .\sync_backend.py --json cloud-backup
```

Then upload images and attachments:

```powershell
py -3 .\sync_backend.py --json cloud-upload-attachments
```

The command fails before Storage writes when a referenced Thread or Session has not
been uploaded.

## Eligible Content

The uploader accepts Probe records that have:

- available local or embedded bytes;
- a verified file size;
- a SHA256 content hash;
- a reference that is not marked pending removal.

Supported content is not restricted to a hard-coded extension list. Known image and
document suffixes such as PNG, JPG, JPEG, WEBP, GIF, PDF, TXT, MD, CSV, JSON, XLSX,
DOCX, PPTX, and ZIP are preserved. Unknown or unsafe suffixes use `.bin`.

Remote-only URLs, missing paths, and `pendingRemovalPaths` are reported as skipped
references and are not uploaded.

## Content Addressing

Attachment entities are unique per `(user_id, sha256)`. Storage objects use:

```text
users/USER_ID/attachments/HASH_PREFIX/SHA256.ext
```

The uploader groups all Probe records by SHA256. For example, a `data:` image and the
matching `event_msg.local_images[]` file become:

- one private Storage object;
- one `attachments` entity;
- two `attachment_references` rows.

If the SHA256 already exists in the cloud, the object is reused and no binary upload is
performed.

## Metadata and References

The `attachments` entity stores representative metadata:

- Thread and Session cloud IDs when known;
- message ID;
- file name, extension, MIME, and size;
- SHA256 and Storage path;
- original local path and reference location;
- reference kind and upload time;
- source device.

Every Probe occurrence is separately upserted into `attachment_references`, preserving
the Thread, Session, message, original path, kind, and JSONL or manifest location.

## Safety

- Embedded `data:` bytes are retained only inside the in-process Probe record.
- `probe-attachments` JSON output never includes embedded content.
- Local files are read with a stable size/mtime check.
- Content is re-hashed immediately before upload.
- The desktop client uses only the signed-in user's Auth session and private RLS paths.
- Missing cloud dependencies cause a pre-upload failure.

After upserts, the client reloads cloud attachment and reference manifests and verifies
all expected hashes and reference locations. Only then is the device backup timestamp
updated.

## Phase Boundary

This phase uploads attachment content and relationships. Phase 9 downloads attachments,
chooses new local paths, rewrites Session references, verifies restored hashes, and
rolls back failed attachment restores.
