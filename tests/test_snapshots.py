from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from codex_sync.cloud.snapshots import SupabaseSnapshotRepository
from codex_sync.cloud.supabase_client import HttpResponse, SupabaseClient
from codex_sync.config import SupabaseConfig
from codex_sync.hashing import sha256_bytes
from codex_sync.models import AuthSession, DeviceIdentity
from codex_sync.versioning import SnapshotManager
from codex_sync.versioning.snapshots import SnapshotRestoreRepository

USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEVICE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SNAPSHOT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


class FakeAuth:
    def restore_session(self) -> AuthSession:
        return AuthSession.from_payload(
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_in": 3600,
                "user": {"id": USER_ID, "email": "user@example.com"},
            }
        )


class FakeDevices:
    def current_device(self) -> DeviceIdentity:
        return DeviceIdentity(
            id=DEVICE_ID,
            device_name="Test",
            os_name="Windows",
            os_version="11",
            client_version="2.0.0",
            first_registered_at="2026-08-21T00:00:00+00:00",
        )

    def register_current_device(self) -> dict[str, object]:
        return self.current_device().to_dict()


class Source:
    def list_threads(self, user_id, access_token, codex_thread_id=None, workspace_id=None):
        rows = [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "codex_thread_id": "thread-1",
                "workspace_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "title": "First",
            },
            {
                "id": "22222222-2222-4222-8222-222222222222",
                "codex_thread_id": "thread-2",
                "workspace_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                "title": "Second",
            },
        ]
        if workspace_id:
            rows = [row for row in rows if row["workspace_id"] == workspace_id]
        return rows

    def list_sessions(self, user_id, access_token):
        return [
            {
                "id": "33333333-3333-4333-8333-333333333333",
                "thread_id": "11111111-1111-4111-8111-111111111111",
                "relative_path": "sessions/thread-1.jsonl",
                "content_hash": "a" * 64,
                "storage_path": f"users/{USER_ID}/sessions/{'a' * 64}.jsonl",
            }
        ]

    def list_attachments(self, user_id, access_token):
        return [
            {
                "id": "44444444-4444-4444-8444-444444444444",
                "sha256": "b" * 64,
                "storage_path": f"users/{USER_ID}/attachments/bb/{'b' * 64}.pdf",
            }
        ]

    def list_references(self, user_id, access_token):
        return [{"id": "55555555-5555-4555-8555-555555555555"}]


class MemorySnapshotRepository:
    def __init__(self, *, fail_upload: bool = False) -> None:
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.manifests: dict[str, bytes] = {}
        self.items: list[dict[str, Any]] = []
        self.fail_upload = fail_upload

    def upsert_snapshot(self, user_id, row, access_token):
        stored = {"user_id": user_id, **row}
        self.snapshots[str(row["id"])] = stored
        return stored

    def update_snapshot(self, user_id, snapshot_id, row, access_token):
        self.snapshots[snapshot_id].update(row)
        return self.snapshots[snapshot_id]

    def upsert_snapshot_items(self, user_id, rows, access_token):
        self.items.extend(rows)
        return rows

    def list_snapshots(self, user_id, access_token):
        return list(self.snapshots.values())

    def upload_manifest(self, object_path, content, access_token):
        if self.fail_upload:
            raise RuntimeError("network unavailable")
        self.manifests[object_path] = content

    def download_manifest(self, object_path, access_token):
        return self.manifests[object_path]


class BaseRestoreRepository:
    def download_session(self, storage_path, access_token):
        return b"session"


class SnapshotTests(unittest.TestCase):
    def test_snapshot_manifest_is_hashed_and_can_filter_workspace(self) -> None:
        repository = MemorySnapshotRepository()
        manager = SnapshotManager(
            FakeAuth(),
            FakeDevices(),
            repository,
            Source(),
            BaseRestoreRepository(),
        )

        snapshot = manager.create(snapshot_type="manual", label="release")
        snapshot_id = str(snapshot["id"])
        storage_path = str(snapshot["manifest_storage_path"])
        content = repository.manifests[storage_path]
        manifest = json.loads(content.decode("utf-8"))

        self.assertEqual(snapshot["status"], "complete")
        self.assertEqual(snapshot["manifest_hash"], sha256_bytes(content))
        self.assertEqual(snapshot["thread_count"], 2)
        self.assertEqual(snapshot["session_count"], 1)
        self.assertEqual(snapshot["attachment_count"], 1)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(len(repository.items), 4)

        restore_repository = manager.load_restore_repository(snapshot_id)
        self.assertEqual(len(restore_repository.list_threads(USER_ID, "token")), 2)
        self.assertEqual(
            len(
                restore_repository.list_threads(
                    USER_ID,
                    "token",
                    workspace_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                )
            ),
            1,
        )
        with self.assertRaisesRegex(RuntimeError, "not in the selected manifest"):
            restore_repository.download_session(
                f"users/{USER_ID}/sessions/{'c' * 64}.jsonl",
                "token",
            )

    def test_snapshot_marks_failure_when_manifest_upload_fails(self) -> None:
        repository = MemorySnapshotRepository(fail_upload=True)
        manager = SnapshotManager(
            FakeAuth(),
            FakeDevices(),
            repository,
            Source(),
            BaseRestoreRepository(),
        )

        with self.assertRaisesRegex(RuntimeError, "Snapshot creation failed"):
            manager.create()

        self.assertEqual(len(repository.snapshots), 1)
        self.assertEqual(next(iter(repository.snapshots.values()))["status"], "failed")


class RecordingTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": json.loads(body.decode("utf-8")) if body else None,
            }
        )
        return self.responses.pop(0)


def response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


class SupabaseSnapshotRepositoryTests(unittest.TestCase):
    def test_snapshot_rest_contract_uses_owned_tables_and_storage_prefix(self) -> None:
        transport = RecordingTransport(
            [
                response(201, [{"id": SNAPSHOT_ID, "status": "pending"}]),
                response(200, {"status": "complete"}),
                response(201, [{"id": "item-id"}]),
                response(200, [{"id": SNAPSHOT_ID, "status": "complete"}]),
                response(200, {"Key": "stored"}),
            ]
        )
        repository = SupabaseSnapshotRepository(
            SupabaseClient(
                SupabaseConfig("https://example.supabase.co", "sb_publishable_example"),
                transport=transport,
            )
        )

        repository.upsert_snapshot(
            USER_ID,
            {"id": SNAPSHOT_ID, "snapshot_type": "manual", "status": "pending"},
            "access-token",
        )
        repository.update_snapshot(
            USER_ID,
            SNAPSHOT_ID,
            {"status": "complete"},
            "access-token",
        )
        repository.upsert_snapshot_items(
            USER_ID,
            [
                {
                    "snapshot_id": SNAPSHOT_ID,
                    "object_type": "session",
                    "object_id": "33333333-3333-4333-8333-333333333333",
                }
            ],
            "access-token",
        )
        repository.list_snapshots(USER_ID, "access-token")
        repository.upload_manifest(
            f"users/{USER_ID}/snapshots/{SNAPSHOT_ID}/manifest.json",
            b"{}",
            "access-token",
        )

        self.assertIn("on_conflict=user_id%2Cid", transport.requests[0]["url"])
        self.assertIn("/rest/v1/snapshot_items", transport.requests[2]["url"])
        self.assertIn(
            f"/storage/v1/object/codex-history-sync/users/{USER_ID}/snapshots/{SNAPSHOT_ID}/manifest.json",
            transport.requests[4]["url"],
        )
        for request in transport.requests:
            self.assertNotIn("access-token", json.dumps(request["body"]))


if __name__ == "__main__":
    unittest.main()
