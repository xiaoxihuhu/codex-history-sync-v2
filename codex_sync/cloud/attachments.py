from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

from codex_sync.cloud.storage import StorageObjectSource, SupabaseStorage
from codex_sync.cloud.supabase_client import SupabaseClient, SupabaseError


class AttachmentRepository(Protocol):
    def list_threads(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def list_sessions(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def download_session(self, storage_path: str, access_token: str) -> bytes: ...

    def list_attachments(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def list_references(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def upload_object(
        self,
        object_path: str,
        content: bytes,
        mime_type: str,
        access_token: str,
    ) -> None: ...

    def upload_source(
        self,
        user_id: str,
        object_path: str,
        source: StorageObjectSource,
        mime_type: str,
        access_token: str,
    ) -> str: ...

    def download_object(self, object_path: str, access_token: str) -> bytes: ...

    def download_object_to_path(
        self,
        user_id: str,
        object_path: str,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
        access_token: str,
    ) -> None: ...

    def upsert_attachments(
        self,
        user_id: str,
        device_id: str,
        rows: list[dict[str, Any]],
        access_token: str,
    ) -> dict[str, str]: ...

    def upsert_references(
        self,
        user_id: str,
        rows: list[dict[str, Any]],
        access_token: str,
    ) -> list[dict[str, Any]]: ...

    def delete_references(
        self,
        user_id: str,
        reference_ids: list[str],
        access_token: str,
    ) -> int: ...


class SupabaseAttachmentRepository:
    def __init__(self, client: SupabaseClient, storage: SupabaseStorage | None = None) -> None:
        self.client = client
        self.storage = storage or SupabaseStorage(client)

    def _list(self, table: str, select: str, user_id: str, access_token: str) -> list[dict[str, Any]]:
        query = urlencode({"select": select, "user_id": f"eq.{user_id}"})
        response = self.client.request_json(
            "GET",
            f"/rest/v1/{table}?{query}",
            access_token=access_token,
            expected_statuses=(200,),
        )
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise SupabaseError(f"Supabase {table} list returned an invalid response")
        return response

    def list_threads(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        return self._list("threads", "id,codex_thread_id", user_id, access_token)

    def list_sessions(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        return self._list(
            "sessions",
            (
                "id,thread_id,codex_session_id,relative_path,content_hash,file_size,"
                "storage_path"
            ),
            user_id,
            access_token,
        )

    def list_attachments(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        return self._list(
            "attachments",
            (
                "id,thread_id,session_id,sha256,file_name,file_extension,mime_type,"
                "file_size,storage_path,original_local_path"
            ),
            user_id,
            access_token,
        )

    def list_references(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        return self._list(
            "attachment_references",
            (
                "id,attachment_id,thread_id,session_id,message_id,reference_kind,"
                "reference_location,original_local_path"
            ),
            user_id,
            access_token,
        )

    def upload_object(
        self,
        object_path: str,
        content: bytes,
        mime_type: str,
        access_token: str,
    ) -> None:
        self.storage.upload(
            object_path,
            content,
            content_type=mime_type or "application/octet-stream",
            access_token=access_token,
        )

    def upload_source(
        self,
        user_id: str,
        object_path: str,
        source: StorageObjectSource,
        mime_type: str,
        access_token: str,
    ) -> str:
        return self.storage.upload_source(
            user_id=user_id,
            legacy_object_path=object_path,
            source=source,
            content_type=mime_type or "application/octet-stream",
            access_token=access_token,
            object_kind="Attachment",
        )

    def download_object(self, object_path: str, access_token: str) -> bytes:
        return self.storage.download(object_path, access_token=access_token)

    def download_session(self, storage_path: str, access_token: str) -> bytes:
        return self.storage.download(storage_path, access_token=access_token)

    def download_object_to_path(
        self,
        user_id: str,
        object_path: str,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
        access_token: str,
    ) -> None:
        self.storage.download_to_path(
            user_id=user_id,
            storage_path=object_path,
            destination=destination,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            access_token=access_token,
            object_kind="Attachment",
        )

    def upsert_attachments(
        self,
        user_id: str,
        device_id: str,
        rows: list[dict[str, Any]],
        access_token: str,
    ) -> dict[str, str]:
        if not rows:
            return {}
        payload = [
            {
                **row,
                "user_id": user_id,
                "source_device_id": device_id,
            }
            for row in rows
        ]
        query = urlencode({"on_conflict": "user_id,sha256"})
        response = self.client.request_json(
            "POST",
            f"/rest/v1/attachments?{query}",
            payload=payload,
            access_token=access_token,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            expected_statuses=(200, 201),
        )
        if not isinstance(response, list):
            raise SupabaseError("Supabase Attachment upsert returned an invalid response")
        mapping = {
            str(item["sha256"]): str(item["id"])
            for item in response
            if isinstance(item, dict) and item.get("sha256") and item.get("id")
        }
        if len(mapping) != len(rows):
            raise SupabaseError("Supabase Attachment upsert did not return every entity")
        return mapping

    def upsert_references(
        self,
        user_id: str,
        rows: list[dict[str, Any]],
        access_token: str,
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        payload = [{**row, "user_id": user_id} for row in rows]
        query = urlencode({"on_conflict": "user_id,attachment_id,reference_location"})
        response = self.client.request_json(
            "POST",
            f"/rest/v1/attachment_references?{query}",
            payload=payload,
            access_token=access_token,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            expected_statuses=(200, 201),
        )
        if not isinstance(response, list) or len(response) != len(rows):
            raise SupabaseError("Supabase Attachment reference upsert returned an incomplete response")
        return response

    def delete_references(
        self,
        user_id: str,
        reference_ids: list[str],
        access_token: str,
    ) -> int:
        ids = sorted({str(value).strip() for value in reference_ids if str(value).strip()})
        if not ids:
            return 0
        query = urlencode(
            {
                "user_id": f"eq.{user_id}",
                "id": f"in.({','.join(ids)})",
            }
        )
        response = self.client.request_json(
            "DELETE",
            f"/rest/v1/attachment_references?{query}",
            access_token=access_token,
            extra_headers={"Prefer": "return=representation"},
            expected_statuses=(200, 204),
        )
        if response in ({}, None):
            return 0
        if not isinstance(response, list):
            raise SupabaseError("Supabase Attachment reference delete returned an invalid response")
        return len(response)
