from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlencode

from codex_sync.cloud.supabase_client import SupabaseClient, SupabaseError


class WorkspaceRepository(Protocol):
    def upsert_workspaces(
        self,
        user_id: str,
        rows: list[dict[str, str]],
        access_token: str,
    ) -> list[dict[str, Any]]: ...

    def upsert_device_workspaces(
        self,
        user_id: str,
        device_id: str,
        rows: list[dict[str, str]],
        access_token: str,
    ) -> list[dict[str, Any]]: ...

    def list_workspaces(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def list_device_workspaces(
        self,
        user_id: str,
        device_id: str,
        access_token: str,
    ) -> list[dict[str, Any]]: ...


class SupabaseWorkspaceRepository:
    def __init__(self, client: SupabaseClient) -> None:
        self.client = client

    def upsert_workspaces(
        self,
        user_id: str,
        rows: list[dict[str, str]],
        access_token: str,
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        payload = [{"user_id": user_id, **row} for row in rows]
        query = urlencode({"on_conflict": "user_id,id"})
        response = self.client.request_json(
            "POST",
            f"/rest/v1/workspaces?{query}",
            payload=payload,
            access_token=access_token,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            expected_statuses=(200, 201),
        )
        return self._validated_rows(response, len(rows), "Workspace upsert")

    def upsert_device_workspaces(
        self,
        user_id: str,
        device_id: str,
        rows: list[dict[str, str]],
        access_token: str,
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        payload = [
            {
                "user_id": user_id,
                "device_id": device_id,
                "workspace_id": row["workspace_id"],
                "local_path": row["local_path"],
            }
            for row in rows
        ]
        query = urlencode({"on_conflict": "device_id,workspace_id"})
        response = self.client.request_json(
            "POST",
            f"/rest/v1/device_workspaces?{query}",
            payload=payload,
            access_token=access_token,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            expected_statuses=(200, 201),
        )
        return self._validated_rows(response, len(rows), "Device Workspace upsert")

    def list_workspaces(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        query = urlencode(
            {
                "select": "id,name,created_at,updated_at",
                "user_id": f"eq.{user_id}",
                "order": "name.asc,id.asc",
            }
        )
        response = self.client.request_json(
            "GET",
            f"/rest/v1/workspaces?{query}",
            access_token=access_token,
            expected_statuses=(200,),
        )
        return self._validated_rows(response, None, "Workspace list")

    def list_device_workspaces(
        self,
        user_id: str,
        device_id: str,
        access_token: str,
    ) -> list[dict[str, Any]]:
        query = urlencode(
            {
                "select": "workspace_id,local_path,updated_at",
                "user_id": f"eq.{user_id}",
                "device_id": f"eq.{device_id}",
                "order": "updated_at.desc",
            }
        )
        response = self.client.request_json(
            "GET",
            f"/rest/v1/device_workspaces?{query}",
            access_token=access_token,
            expected_statuses=(200,),
        )
        return self._validated_rows(response, None, "Device Workspace list")

    @staticmethod
    def _validated_rows(
        response: object,
        expected_count: int | None,
        operation: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise SupabaseError(f"Supabase {operation} returned an invalid response")
        if expected_count is not None and len(response) != expected_count:
            raise SupabaseError(f"Supabase {operation} returned an incomplete response")
        return response
