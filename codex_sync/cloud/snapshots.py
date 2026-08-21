from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlencode

from codex_sync.cloud.storage import SupabaseStorage
from codex_sync.cloud.supabase_client import SupabaseClient, SupabaseError


class SnapshotRepository(Protocol):
    def upsert_snapshot(
        self,
        user_id: str,
        row: dict[str, Any],
        access_token: str,
    ) -> dict[str, Any]: ...

    def update_snapshot(
        self,
        user_id: str,
        snapshot_id: str,
        row: dict[str, Any],
        access_token: str,
    ) -> dict[str, Any]: ...

    def upsert_snapshot_items(
        self,
        user_id: str,
        rows: list[dict[str, Any]],
        access_token: str,
    ) -> list[dict[str, Any]]: ...

    def list_snapshots(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def upload_manifest(
        self,
        object_path: str,
        content: bytes,
        access_token: str,
    ) -> None: ...

    def download_manifest(self, object_path: str, access_token: str) -> bytes: ...


class SupabaseSnapshotRepository:
    def __init__(self, client: SupabaseClient, storage: SupabaseStorage | None = None) -> None:
        self.client = client
        self.storage = storage or SupabaseStorage(client)

    def upsert_snapshot(
        self,
        user_id: str,
        row: dict[str, Any],
        access_token: str,
    ) -> dict[str, Any]:
        payload = {"user_id": user_id, **row}
        query = urlencode({"on_conflict": "user_id,id"})
        response = self.client.request_json(
            "POST",
            f"/rest/v1/snapshots?{query}",
            payload=payload,
            access_token=access_token,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            expected_statuses=(200, 201),
        )
        return self._one(response, "Snapshot upsert")

    def update_snapshot(
        self,
        user_id: str,
        snapshot_id: str,
        row: dict[str, Any],
        access_token: str,
    ) -> dict[str, Any]:
        query = urlencode({"user_id": f"eq.{user_id}", "id": f"eq.{snapshot_id}"})
        response = self.client.request_json(
            "PATCH",
            f"/rest/v1/snapshots?{query}",
            payload=row,
            access_token=access_token,
            extra_headers={"Prefer": "return=representation"},
            expected_statuses=(200, 204),
        )
        if response in ({}, None):
            return {"id": snapshot_id, **row}
        return self._one(response, "Snapshot update")

    def upsert_snapshot_items(
        self,
        user_id: str,
        rows: list[dict[str, Any]],
        access_token: str,
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        payload = [{"user_id": user_id, **row} for row in rows]
        query = urlencode({"on_conflict": "user_id,snapshot_id,object_type,object_id"})
        response = self.client.request_json(
            "POST",
            f"/rest/v1/snapshot_items?{query}",
            payload=payload,
            access_token=access_token,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            expected_statuses=(200, 201),
        )
        if not isinstance(response, list) or len(response) != len(rows):
            raise SupabaseError("Supabase Snapshot Item upsert returned an incomplete response")
        return [item for item in response if isinstance(item, dict)]

    def list_snapshots(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        query = urlencode(
            {
                "select": (
                    "id,source_device_id,workspace_id,snapshot_type,label,status,"
                    "manifest_hash,manifest_storage_path,thread_count,session_count,"
                    "attachment_count,completed_at,created_at"
                ),
                "user_id": f"eq.{user_id}",
                "order": "created_at.desc,id.desc",
            }
        )
        response = self.client.request_json(
            "GET",
            f"/rest/v1/snapshots?{query}",
            access_token=access_token,
            expected_statuses=(200,),
        )
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise SupabaseError("Supabase Snapshot list returned an invalid response")
        return response

    def upload_manifest(
        self,
        object_path: str,
        content: bytes,
        access_token: str,
    ) -> None:
        self.storage.upload(
            object_path,
            content,
            content_type="application/json",
            access_token=access_token,
        )

    def download_manifest(self, object_path: str, access_token: str) -> bytes:
        return self.storage.download(object_path, access_token=access_token)

    @staticmethod
    def _one(response: object, operation: str) -> dict[str, Any]:
        if isinstance(response, list) and response and isinstance(response[0], dict):
            return response[0]
        if isinstance(response, dict) and response:
            return response
        raise SupabaseError(f"Supabase {operation} returned an invalid response")
