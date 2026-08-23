from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from codex_sync.attachments.probe import AttachmentProbeRecord, probe_attachments
from codex_sync.cloud.attachments import AttachmentRepository
from codex_sync.cloud.auth import AuthService
from codex_sync.cloud.storage import StorageObjectSource
from codex_sync.cloud.devices import DeviceService
from codex_sync.hashing import sha256_bytes
from codex_sync.local.catalog import read_stable_file
from codex_sync.local.repair_engine import Paths
from codex_sync.local.restore_engine import safe_restore_path
from codex_sync.progress import emit_progress

UTC = timezone.utc
SAFE_EXTENSION_PATTERN = re.compile(r"^\.[a-z0-9]{1,16}$")


@dataclass(frozen=True)
class AttachmentUploadSummary:
    scanned_references: int
    eligible_references: int
    skipped_missing_references: int
    unique_attachments: int
    uploaded_objects: int
    reused_objects: int
    upserted_attachments: int
    upserted_references: int
    verified_attachments: int
    verified_references: int
    probe_issues: int
    stale_references_removed: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def storage_extension(record: AttachmentProbeRecord) -> str:
    suffix = Path(record.file_name).suffix.lower()
    if SAFE_EXTENSION_PATTERN.fullmatch(suffix):
        return suffix
    return ".bin"


def attachment_source(
    records: list[AttachmentProbeRecord],
    expected_hash: str,
) -> StorageObjectSource:
    for record in records:
        if record.embedded_content is not None:
            content = record.embedded_content
            if sha256_bytes(content) != expected_hash:
                raise RuntimeError(f"Embedded Attachment changed during scan: {record.reference_location}")
            return StorageObjectSource.from_bytes(
                content,
                expected_hash,
                display_path=record.local_path or record.file_name,
            )
    for record in records:
        if record.local_path and record.exists:
            stable = read_stable_file(Path(record.local_path), load_content=False)
            if stable.sha256 != expected_hash:
                raise RuntimeError(f"Attachment changed during scan: {record.local_path}")
            return StorageObjectSource.from_path(
                stable.path,
                stable.sha256,
                stable.file_size,
            )
    raise RuntimeError(f"No readable content exists for Attachment {expected_hash}")


def representative_record(records: list[AttachmentProbeRecord]) -> AttachmentProbeRecord:
    return max(
        records,
        key=lambda item: (
            item.thread_id is not None,
            item.session_id is not None,
            item.message_id is not None,
            item.local_path is not None,
        ),
    )


class AttachmentUploadEngine:
    def __init__(
        self,
        paths: Paths,
        auth: AuthService,
        devices: DeviceService,
        repository: AttachmentRepository,
    ) -> None:
        self.paths = paths
        self.auth = auth
        self.devices = devices
        self.repository = repository

    def upload(self) -> AttachmentUploadSummary:
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before uploading attachments")
        device = self.devices.current_device()
        self.devices.register_current_device()
        result = probe_attachments(self.paths.codex_home, include_archived=True)
        emit_progress(
            "scan",
            object_kind="Attachment",
            attachments=len(result.attachments),
        )
        eligible = [
            item
            for item in result.attachments
            if item.exists
            and item.sha256
            and item.file_size is not None
            and item.reference_kind != "pending_removal_manifest"
        ]
        skipped = len(result.attachments) - len(eligible)

        thread_rows = self.repository.list_threads(session.user.id, session.access_token)
        session_rows = self.repository.list_sessions(session.user.id, session.access_token)
        thread_ids = {
            str(item["codex_thread_id"]): str(item["id"])
            for item in thread_rows
            if item.get("codex_thread_id") and item.get("id")
        }
        session_ids = {
            str(item["codex_session_id"]): str(item["id"])
            for item in session_rows
            if item.get("codex_session_id") and item.get("id")
        }
        session_thread_ids = {
            str(item["codex_session_id"]): str(item["thread_id"])
            for item in session_rows
            if item.get("codex_session_id") and item.get("thread_id")
        }
        for record in eligible:
            if record.thread_id and record.thread_id not in thread_ids:
                raise RuntimeError(
                    f"Attachment Thread is not uploaded yet: {record.thread_id}"
                )
            if record.session_id and record.session_id not in session_ids:
                raise RuntimeError(
                    f"Attachment Session is not uploaded yet: {record.session_id}"
                )

        grouped: dict[str, list[AttachmentProbeRecord]] = {}
        for record in eligible:
            grouped.setdefault(str(record.sha256), []).append(record)

        existing_rows = self.repository.list_attachments(
            session.user.id,
            session.access_token,
        )
        existing = {
            str(item["sha256"]): item
            for item in existing_rows
            if item.get("sha256") and item.get("id")
        }
        uploaded_objects = 0
        reused_objects = 0
        attachment_rows: list[dict[str, object]] = []
        uploaded_at = datetime.now(tz=UTC).isoformat()

        for digest, records in grouped.items():
            representative = representative_record(records)
            extension = storage_extension(representative)
            object_path = f"users/{session.user.id}/attachments/{digest[:2]}/{digest}{extension}"
            if digest in existing:
                object_path = str(existing[digest].get("storage_path") or object_path)
                reused_objects += 1
            else:
                source = attachment_source(records, digest)
                object_path = self.repository.upload_source(
                    session.user.id,
                    object_path,
                    source,
                    representative.mime_type,
                    session.access_token,
                )
                uploaded_objects += 1
            cloud_thread_id = (
                thread_ids.get(representative.thread_id)
                if representative.thread_id
                else None
            )
            cloud_session_id = (
                session_ids.get(representative.session_id)
                if representative.session_id
                else None
            )
            if cloud_thread_id is None and representative.session_id:
                cloud_thread_id = session_thread_ids.get(representative.session_id)
            attachment_rows.append(
                {
                    "thread_id": cloud_thread_id,
                    "session_id": cloud_session_id,
                    "message_id": representative.message_id,
                    "sha256": digest,
                    "file_name": representative.file_name[:1024],
                    "file_extension": extension,
                    "mime_type": representative.mime_type,
                    "file_size": representative.file_size,
                    "storage_path": object_path,
                    "original_local_path": representative.local_path,
                    "original_reference_location": representative.reference_location,
                    "reference_kind": representative.reference_kind,
                    "last_uploaded_at": uploaded_at,
                }
            )

        attachment_ids = self.repository.upsert_attachments(
            session.user.id,
            device.id,
            attachment_rows,
            session.access_token,
        )
        existing_references = self.repository.list_references(
            session.user.id,
            session.access_token,
        )
        reference_rows: list[dict[str, object]] = []
        expected_reference_keys: dict[str, set[tuple[str, str]]] = {}
        for record in eligible:
            digest = str(record.sha256)
            cloud_session_id = session_ids.get(record.session_id) if record.session_id else None
            cloud_thread_id = thread_ids.get(record.thread_id) if record.thread_id else None
            if cloud_thread_id is None and record.session_id:
                cloud_thread_id = session_thread_ids.get(record.session_id)
            attachment_id = attachment_ids[digest]
            reference_rows.append(
                {
                    "attachment_id": attachment_id,
                    "thread_id": cloud_thread_id,
                    "session_id": cloud_session_id,
                    "message_id": record.message_id,
                    "reference_kind": record.reference_kind,
                    "reference_location": record.reference_location,
                    "original_local_path": record.local_path,
                }
            )
            if cloud_session_id:
                expected_reference_keys.setdefault(cloud_session_id, set()).add(
                    (attachment_id, record.reference_location)
                )
        self.repository.upsert_references(
            session.user.id,
            reference_rows,
            session.access_token,
        )

        covered_cloud_session_ids: set[str] = set()
        for row in session_rows:
            cloud_session_id = str(row.get("id") or "")
            relative_path = str(row.get("relative_path") or "")
            if not cloud_session_id or not relative_path:
                continue
            try:
                local_session_path = safe_restore_path(
                    self.paths.codex_home,
                    relative_path,
                )
            except RuntimeError:
                continue
            if local_session_path.is_file():
                covered_cloud_session_ids.add(cloud_session_id)

        stale_reference_ids = [
            str(row["id"])
            for row in existing_references
            if row.get("id")
            and str(row.get("session_id") or "") in covered_cloud_session_ids
            and (
                str(row.get("attachment_id") or ""),
                str(row.get("reference_location") or ""),
            )
            not in expected_reference_keys.get(str(row.get("session_id") or ""), set())
        ]
        delete_references = getattr(self.repository, "delete_references", None)
        if stale_reference_ids:
            if not callable(delete_references):
                raise RuntimeError("Attachment reference reconciliation is not supported")
            stale_references_removed = int(
                delete_references(
                    session.user.id,
                    stale_reference_ids,
                    session.access_token,
                )
            )
            if stale_references_removed != len(stale_reference_ids):
                raise RuntimeError(
                    "Cloud Attachment reference reconciliation removed an incomplete set"
                )
        else:
            stale_references_removed = 0

        verified_entities = self.repository.list_attachments(
            session.user.id,
            session.access_token,
        )
        verified_hashes = {
            str(item.get("sha256"))
            for item in verified_entities
            if item.get("sha256")
        }
        missing_hashes = set(grouped) - verified_hashes
        if missing_hashes:
            raise RuntimeError(
                f"Cloud verification found {len(missing_hashes)} missing Attachments"
            )
        verified_references = self.repository.list_references(
            session.user.id,
            session.access_token,
        )
        verified_locations = {
            str(item.get("reference_location"))
            for item in verified_references
            if item.get("reference_location")
        }
        expected_locations = {item.reference_location for item in eligible}
        missing_locations = expected_locations - verified_locations
        if missing_locations:
            raise RuntimeError(
                f"Cloud verification found {len(missing_locations)} missing Attachment references"
            )
        self.devices.record_successful_backup()

        return AttachmentUploadSummary(
            scanned_references=len(result.attachments),
            eligible_references=len(eligible),
            skipped_missing_references=skipped,
            unique_attachments=len(grouped),
            uploaded_objects=uploaded_objects,
            reused_objects=reused_objects,
            upserted_attachments=len(attachment_rows),
            upserted_references=len(reference_rows),
            verified_attachments=len(grouped),
            verified_references=len(expected_locations),
            probe_issues=len(result.issues),
            stale_references_removed=stale_references_removed,
        )
