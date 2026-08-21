from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlencode

from codex_sync import __version__
from codex_sync.cloud.auth import AuthService
from codex_sync.cloud.supabase_client import SupabaseClient, SupabaseError
from codex_sync.models import DeviceIdentity
from codex_sync.sync.state import SyncStateStore


class DeviceRepository(Protocol):
    def upsert(self, user_id: str, device: DeviceIdentity, access_token: str) -> dict[str, Any]: ...

    def list(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...


class SupabaseDeviceRepository:
    def __init__(self, client: SupabaseClient) -> None:
        self.client = client

    def upsert(self, user_id: str, device: DeviceIdentity, access_token: str) -> dict[str, Any]:
        query = urlencode({"on_conflict": "user_id,id"})
        payload = {
            "id": device.id,
            "user_id": user_id,
            "device_name": device.device_name,
            "os_name": device.os_name,
            "os_version": device.os_version,
            "client_version": device.client_version,
            "first_registered_at": device.first_registered_at,
            "last_seen_at": device.last_seen_at,
            "last_backup_at": device.last_backup_at,
        }
        response = self.client.request_json(
            "POST",
            f"/rest/v1/devices?{query}",
            payload=payload,
            access_token=access_token,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            expected_statuses=(200, 201),
        )
        if not isinstance(response, list) or not response or not isinstance(response[0], dict):
            raise SupabaseError("Supabase device upsert returned an invalid response")
        return response[0]

    def list(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        query = urlencode(
            {
                "select": (
                    "id,device_name,os_name,os_version,client_version,"
                    "first_registered_at,last_seen_at,last_backup_at"
                ),
                "user_id": f"eq.{user_id}",
                "order": "last_seen_at.desc.nullslast",
            }
        )
        response = self.client.request_json(
            "GET",
            f"/rest/v1/devices?{query}",
            access_token=access_token,
            expected_statuses=(200,),
        )
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise SupabaseError("Supabase device list returned an invalid response")
        return response


class DeviceService:
    def __init__(
        self,
        auth: AuthService,
        state: SyncStateStore,
        repository: DeviceRepository,
    ) -> None:
        self.auth = auth
        self.state = state
        self.repository = repository

    def current_device(self) -> DeviceIdentity:
        return self.state.get_or_create_device(__version__)

    def register_current_device(self) -> dict[str, Any]:
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before registering this device")
        self.state.get_or_create_device(__version__)
        device = self.state.mark_device_seen()
        return self.repository.upsert(session.user.id, device, session.access_token)

    def list_devices(self) -> list[dict[str, Any]]:
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before listing devices")
        return self.repository.list(session.user.id, session.access_token)
