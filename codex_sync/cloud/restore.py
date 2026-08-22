from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

from codex_sync.cloud.storage import SupabaseStorage
from codex_sync.cloud.supabase_client import SupabaseClient, SupabaseError


class RestoreRepository(Protocol):
    def list_threads(
        self,
        user_id: str,
        access_token: str,
        codex_thread_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_sessions(
        self,
        user_id: str,
        access_token: str,
    ) -> list[dict[str, Any]]: ...

    def download_session(self, storage_path: str, access_token: str) -> bytes: ...

    def download_session_to_path(
        self,
        user_id: str,
        storage_path: str,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
        access_token: str,
    ) -> None: ...


class SupabaseRestoreRepository:
    def __init__(self, client: SupabaseClient, storage: SupabaseStorage | None = None) -> None:
        self.client = client
        self.storage = storage or SupabaseStorage(client)

    def list_threads(
        self,
        user_id: str,
        access_token: str,
        codex_thread_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query_values = {
            "select": (
                "id,codex_thread_id,workspace_id,rollout_relative_path,title,source,model_provider,"
                "model,original_cwd,archived,codex_created_at,codex_updated_at,metadata"
            ),
            "user_id": f"eq.{user_id}",
            "order": "codex_updated_at.asc.nullsfirst,id.asc",
        }
        if codex_thread_id:
            query_values["codex_thread_id"] = f"eq.{codex_thread_id}"
        if workspace_id:
            query_values["workspace_id"] = f"eq.{workspace_id}"
        response = self.client.request_json(
            "GET",
            f"/rest/v1/threads?{urlencode(query_values)}",
            access_token=access_token,
            expected_statuses=(200,),
        )
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise SupabaseError("Supabase Thread restore manifest is invalid")
        return response

    def list_sessions(
        self,
        user_id: str,
        access_token: str,
    ) -> list[dict[str, Any]]:
        query = urlencode(
            {
                "select": (
                    "id,thread_id,codex_session_id,relative_path,content_hash,file_size,"
                    "storage_path,source_mtime_ns,codex_created_at,codex_updated_at,updated_at"
                ),
                "user_id": f"eq.{user_id}",
                "order": "updated_at.asc,id.asc",
            }
        )
        response = self.client.request_json(
            "GET",
            f"/rest/v1/sessions?{query}",
            access_token=access_token,
            expected_statuses=(200,),
        )
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise SupabaseError("Supabase Session restore manifest is invalid")
        return response

    def download_session(self, storage_path: str, access_token: str) -> bytes:
        return self.storage.download(storage_path, access_token=access_token)

    def download_session_to_path(
        self,
        user_id: str,
        storage_path: str,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
        access_token: str,
    ) -> None:
        self.storage.download_to_path(
            user_id=user_id,
            storage_path=storage_path,
            destination=destination,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            access_token=access_token,
            object_kind="Session",
        )
