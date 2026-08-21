# Attachment Probe

The Phase 3 attachment probe is read-only. It scans known Codex structures
without assuming that every attachment lives in one fixed directory.

## Sources

The probe currently reads:

- Active `sessions/**/*.jsonl`.
- Archived `archived_sessions/**/*.jsonl`.
- `attachments/pasted-text-attachments.json`.
- Structured `response_item` image/file content.
- Structured `event_msg.local_images` and `event_msg.local_audio` fields.
- Codex-generated "Files pasted by the user" message sections.
- Explicit paths under the Codex `attachments` directory.

Arbitrary project paths, source paths, command output paths, and URLs in normal
user text are excluded. This is a deliberate whitelist boundary that prevents
ordinary conversation content from being misclassified as an attachment.

## Output

Each reference reports:

- Thread ID, when known.
- Session ID, when known.
- Message ID, when known.
- File name and type.
- MIME type.
- File size.
- Local path, when the reference resolves to a file.
- SHA256, when content is available.
- JSONL or manifest reference location.
- Reference kind.
- Whether the referenced content currently exists.

The output contains attachment metadata only. Embedded `data:` URI content is
decoded for hashing but is never included in the JSON result.

## Observed Modern Codex Behavior

The anonymous local audit on 2026-08-21 found:

- User image content in `response_item.content[].image_url` as a `data:` URI.
- Matching local PNG paths in `event_msg.local_images[]`.
- The embedded bytes and local PNG bytes have the same SHA256.
- Pasted text under `attachments/<UUID>/pasted-text.txt`.
- A root pasted-text manifest that tracks paths and text previews but does not
  carry thread or message IDs.
- File paths in Codex-generated user attachment sections that can refer to
  files outside the Codex home, including spreadsheets.

These observations are adapter inputs, not permanent format guarantees.

## Command

```powershell
py -3 .\sync_backend.py --json probe-attachments
```

To exclude archived sessions:

```powershell
py -3 .\sync_backend.py --json probe-attachments --active-only
```

## Limitations

- Message IDs are not present in every event shape.
- The current sample does not cover every binary attachment type.
- Remote-only URLs are not treated as local attachment entities.
- Cloud upload and restore are intentionally outside Phase 3.
