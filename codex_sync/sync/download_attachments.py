from __future__ import annotations

import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from codex_sync.cloud.attachments import AttachmentRepository
from codex_sync.cloud.auth import AuthService
from codex_sync.cloud.devices import DeviceService
from codex_sync.hashing import sha256_bytes, sha256_file
from codex_sync.local.attachment_restore import (
    AttachmentRestoreSummary,
    PreparedAttachmentRestore,
    count_session_references,
    replacement_variants,
    restore_attachments_locally,
    restored_attachment_path,
)
from codex_sync.local.repair_engine import Paths, ensure_environment

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AttachmentDownloadSummary:
    cloud_attachments: int
    downloaded_objects: int
    reused_local_objects: int
    local_restore: AttachmentRestoreSummary
    stale_cloud_references_skipped: int = 0

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["local_restore"] = self.local_restore.to_dict()
        return payload


class AttachmentDownloadEngine:
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

    def _stale_reference_ids(
        self,
        references: list[dict[str, object]],
        cloud_sessions: list[dict[str, object]],
        access_token: str,
    ) -> set[str]:
        download_session = getattr(self.repository, "download_session", None)
        if not callable(download_session):
            return set()

        session_rows = {
            str(row.get("id") or ""): row
            for row in cloud_sessions
            if row.get("id")
        }
        references_by_session: dict[str, list[dict[str, object]]] = {}
        for reference in references:
            session_id = str(reference.get("session_id") or "")
            if session_id and reference.get("id"):
                references_by_session.setdefault(session_id, []).append(reference)

        stale_ids: set[str] = set()
        for session_id, session_references in references_by_session.items():
            session_row = session_rows.get(session_id)
            if session_row is None:
                continue
            storage_path = str(session_row.get("storage_path") or "")
            if not storage_path:
                continue
            content = download_session(storage_path, access_token)
            expected_hash = str(session_row.get("content_hash") or "")
            if expected_hash and sha256_bytes(content) != expected_hash:
                raise RuntimeError(
                    f"Cloud Session verification failed before Attachment restore: {storage_path}"
                )
            if session_row.get("file_size") is not None:
                try:
                    expected_size = int(session_row["file_size"])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"Cloud Session has invalid file_size: {storage_path}"
                    ) from exc
                if len(content) != expected_size:
                    raise RuntimeError(
                        f"Cloud Session size verification failed before Attachment restore: "
                        f"{storage_path}"
                    )

            descriptor, temp_name = tempfile.mkstemp(
                prefix="codex-sync-cloud-session-",
                suffix=".jsonl",
            )
            os.close(descriptor)
            temp_path = Path(temp_name)
            try:
                temp_path.write_bytes(content)
                for reference in session_references:
                    original_path = str(reference.get("original_local_path") or "")
                    if not original_path:
                        continue
                    variants = replacement_variants(original_path)
                    occurrences = count_session_references(
                        temp_path,
                        {variant: variant for variant in variants},
                    )
                    if occurrences == 0:
                        stale_ids.add(str(reference["id"]))
            finally:
                temp_path.unlink(missing_ok=True)
        return stale_ids

    def restore(self) -> AttachmentDownloadSummary:
        ensure_environment(self.paths)
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before restoring attachments")
        self.devices.register_current_device()
        attachments = self.repository.list_attachments(session.user.id, session.access_token)
        references = self.repository.list_references(session.user.id, session.access_token)
        cloud_sessions = self.repository.list_sessions(session.user.id, session.access_token)
        stale_reference_ids = self._stale_reference_ids(
            references,
            cloud_sessions,
            session.access_token,
        )
        references_by_attachment: dict[str, list[dict[str, object]]] = {}
        for reference in references:
            attachment_id = str(reference.get("attachment_id") or "")
            if attachment_id:
                references_by_attachment.setdefault(attachment_id, []).append(reference)

        prepared: list[PreparedAttachmentRestore] = []
        downloaded = 0
        reused = 0
        with tempfile.TemporaryDirectory(prefix="codex-sync-attachments-") as temp_dir:
            staging = Path(temp_dir)
            for item in attachments:
                attachment_id = str(item.get("id") or "")
                digest = str(item.get("sha256") or "")
                storage_path = str(item.get("storage_path") or "")
                extension = str(item.get("file_extension") or ".bin")
                if not attachment_id or not SHA256_PATTERN.fullmatch(digest):
                    raise RuntimeError("Cloud Attachment manifest contains an invalid entity")
                expected_prefix = f"users/{session.user.id}/attachments/"
                expected_manifest = (
                    f"users/{session.user.id}/large-objects/{digest}/manifest.json"
                )
                is_legacy = storage_path.startswith(expected_prefix) and digest in storage_path
                if not is_legacy and storage_path != expected_manifest:
                    raise RuntimeError(f"Cloud Attachment has an unsafe Storage path: {digest}")
                try:
                    file_size = int(item.get("file_size"))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(f"Cloud Attachment has invalid file_size: {digest}") from exc

                target = restored_attachment_path(self.paths.codex_home, digest, extension)
                content_path: Path | None = None
                if target.exists():
                    if sha256_file(target) != digest:
                        raise RuntimeError(f"Existing restored Attachment hash mismatch: {digest}")
                    reused += 1
                else:
                    content_path = staging / digest
                    download_to_path = getattr(
                        self.repository,
                        "download_object_to_path",
                        None,
                    )
                    if callable(download_to_path):
                        download_to_path(
                            session.user.id,
                            storage_path,
                            content_path,
                            digest,
                            file_size,
                            session.access_token,
                        )
                    else:
                        content_path.write_bytes(
                            self.repository.download_object(
                                storage_path,
                                session.access_token,
                            )
                        )
                    if content_path.stat().st_size != file_size or sha256_file(content_path) != digest:
                        raise RuntimeError(f"Downloaded Attachment verification failed: {digest}")
                    downloaded += 1
                prepared.append(
                    PreparedAttachmentRestore(
                        attachment_id=attachment_id,
                        sha256=digest,
                        file_extension=extension,
                        file_size=file_size,
                        content=None,
                        references=tuple(references_by_attachment.get(attachment_id, [])),
                        content_path=content_path,
                    )
                )

            local_summary = restore_attachments_locally(
                self.paths,
                prepared,
                cloud_sessions,
                stale_reference_ids,
            )
        return AttachmentDownloadSummary(
            cloud_attachments=len(attachments),
            downloaded_objects=downloaded,
            reused_local_objects=reused,
            local_restore=local_summary,
            stale_cloud_references_skipped=len(stale_reference_ids),
        )
