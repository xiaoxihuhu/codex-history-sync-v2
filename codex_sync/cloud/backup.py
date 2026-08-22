from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlencode

from codex_sync.cloud.storage import StorageObjectSource, SupabaseStorage
from codex_sync.cloud.supabase_client import SupabaseClient, SupabaseError
from codex_sync.local.catalog import LocalThreadRecord, StableFile


class ManualUploadRepository(Protocol):
    def upsert_threads(
        self,
        user_id: str,
        device_id: str,
        threads: list[LocalThreadRecord],
        access_token: str,
        workspace_ids: dict[str, str] | None = None,
    ) -> dict[str, str]: ...

    def list_sessions(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def upload_session_object(
        self,
        user_id: str,
        stable_file: StableFile,
        access_token: str,
    ) -> str: ...

    def upsert_sessions(
        self,
        user_id: str,
        device_id: str,
        rows: list[dict[str, Any]],
        access_token: str,
    ) -> list[dict[str, Any]]: ...


class SupabaseManualUploadRepository:
    def __init__(self, client: SupabaseClient, storage: SupabaseStorage | None = None) -> None:
        self.client = client
        self.storage = storage or SupabaseStorage(client)

    def upsert_threads(
        self,
        user_id: str,
        device_id: str,
        threads: list[LocalThreadRecord],
        access_token: str,
        workspace_ids: dict[str, str] | None = None,
    ) -> dict[str, str]:
        if not threads:
            return {}
        chosen_workspace_ids = workspace_ids or {}
        payload = [
            {
                "user_id": user_id,
                "codex_thread_id": item.codex_thread_id,
                "workspace_id": chosen_workspace_ids.get(item.codex_thread_id),
                "source_device_id": device_id,
                "rollout_relative_path": item.rollout_relative_path,
                "title": item.title,
                "source": item.source,
                "model_provider": item.model_provider,
                "model": item.model,
                "original_cwd": item.original_cwd,
                "archived": item.archived,
                "codex_created_at": item.codex_created_at,
                "codex_updated_at": item.codex_updated_at,
                "metadata": {},
            }
            for item in threads
        ]
        query = urlencode({"on_conflict": "user_id,codex_thread_id"})
        response = self.client.request_json(
            "POST",
            f"/rest/v1/threads?{query}",
            payload=payload,
            access_token=access_token,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            expected_statuses=(200, 201),
        )
        if not isinstance(response, list):
            raise SupabaseError("Supabase thread upsert returned an invalid response")
        mapping = {
            str(item["codex_thread_id"]): str(item["id"])
            for item in response
            if isinstance(item, dict) and item.get("codex_thread_id") and item.get("id")
        }
        if len(mapping) != len(threads):
            raise SupabaseError("Supabase thread upsert did not return every Thread")
        return mapping

    def list_sessions(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        query = urlencode(
            {
                "select": "id,thread_id,relative_path,content_hash,storage_path,file_size",
                "user_id": f"eq.{user_id}",
            }
        )
        response = self.client.request_json(
            "GET",
            f"/rest/v1/sessions?{query}",
            access_token=access_token,
            expected_statuses=(200,),
        )
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise SupabaseError("Supabase session list returned an invalid response")
        return response

    def upload_session_object(
        self,
        user_id: str,
        stable_file: StableFile,
        access_token: str,
    ) -> str:
        object_path = f"users/{user_id}/sessions/{stable_file.sha256}.jsonl"
        return self.storage.upload_source(
            user_id=user_id,
            legacy_object_path=object_path,
            source=StorageObjectSource.from_path(
                stable_file.path,
                stable_file.sha256,
                stable_file.file_size,
            ),
            content_type="application/x-ndjson",
            access_token=access_token,
            object_kind="Session",
        )

    def upsert_sessions(
        self,
        user_id: str,
        device_id: str,
        rows: list[dict[str, Any]],
        access_token: str,
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        payload = [
            {
                **row,
                "user_id": user_id,
                "source_device_id": device_id,
            }
            for row in rows
        ]
        query = urlencode({"on_conflict": "user_id,thread_id,relative_path"})
        response = self.client.request_json(
            "POST",
            f"/rest/v1/sessions?{query}",
            payload=payload,
            access_token=access_token,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            expected_statuses=(200, 201),
        )
        if not isinstance(response, list) or len(response) != len(rows):
            raise SupabaseError("Supabase session upsert returned an incomplete response")
        return response
