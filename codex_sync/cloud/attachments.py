from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlencode

from codex_sync.cloud.storage import SupabaseStorage
from codex_sync.cloud.supabase_client import SupabaseClient, SupabaseError


class AttachmentRepository(Protocol):
    def list_threads(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def list_sessions(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def list_attachments(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def list_references(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def upload_object(
        self,
        object_path: str,
        content: bytes,
        mime_type: str,
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
            "id,thread_id,codex_session_id",
            user_id,
            access_token,
        )

    def list_attachments(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        return self._list(
            "attachments",
            "id,sha256,storage_path,file_size,mime_type",
            user_id,
            access_token,
        )

    def list_references(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        return self._list(
            "attachment_references",
            "id,attachment_id,thread_id,session_id,message_id,reference_location",
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
