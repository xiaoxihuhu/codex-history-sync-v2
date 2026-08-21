from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Mapping

from codex_sync import __version__
from codex_sync.cloud.supabase_client import HttpResponse, SupabaseClient
from codex_sync.cloud.workspaces import SupabaseWorkspaceRepository
from codex_sync.config import AppPaths, SupabaseConfig
from codex_sync.hashing import sha256_bytes
from codex_sync.models import AuthSession, DeviceIdentity
from codex_sync.sync import SyncStateStore
from codex_sync.sync.download import ManualDownloadEngine
from codex_sync.sync.upload import ManualUploadEngine
from codex_sync.workspace import WorkspaceManager, workspace_id_for_path
from tests.test_text_restore import (
    FakeAuth,
    FakeDevices,
    MemoryCloudRepository,
    create_modern_codex_home,
)
from codex_sync.local.repair_engine import resolve_paths


USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEVICE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class XorProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return bytes(value ^ 0xA5 for value in plaintext)

    def unprotect(self, protected: bytes) -> bytes:
        return self.protect(protected)


class MemoryWorkspaceRepository:
    def __init__(self) -> None:
        self.workspaces: dict[str, dict[str, str]] = {}
        self.device_mappings: dict[tuple[str, str], dict[str, str]] = {}

    def upsert_workspaces(self, user_id, rows, access_token):
        for row in rows:
            self.workspaces[row["id"]] = {
                "id": row["id"],
                "name": row["name"],
                "user_id": user_id,
            }
        return list(self.workspaces.values())

    def upsert_device_workspaces(self, user_id, device_id, rows, access_token):
        for row in rows:
            self.device_mappings[(device_id, row["workspace_id"])] = {
                "workspace_id": row["workspace_id"],
                "local_path": row["local_path"],
                "user_id": user_id,
            }
        return list(self.device_mappings.values())

    def list_workspaces(self, user_id, access_token):
        return list(self.workspaces.values())

    def list_device_workspaces(self, user_id, device_id, access_token):
        return [
            row
            for (mapped_device, _), row in self.device_mappings.items()
            if mapped_device == device_id
        ]


def make_paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        config_path=root / "config.json",
        state_db_path=root / "sync_state.sqlite",
        logs_dir=root / "logs",
    )


class WorkspaceMappingTests(unittest.TestCase):
    def test_workspace_id_is_stable_for_windows_case_and_separators(self) -> None:
        first = workspace_id_for_path(
            USER_ID,
            r"C:\Users\AAA\Projects\Shop",
        )
        second = workspace_id_for_path(
            USER_ID,
            r"c:/users/aaa/projects/shop",
        )
        different_user = workspace_id_for_path(
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            r"C:\Users\AAA\Projects\Shop",
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, different_user)

    def test_state_store_persists_workspace_mapping_in_schema_v2(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            state = SyncStateStore(make_paths(root), XorProtector())
            workspace_id = workspace_id_for_path(USER_ID, r"C:\Source\Shop")

            saved = state.save_workspace_mapping(workspace_id, workspace, "Shop")
            restored = SyncStateStore(make_paths(root), XorProtector()).get_workspace_mapping(
                workspace_id
            )

            self.assertEqual(saved, restored)
            self.assertEqual(restored.local_path, str(workspace.resolve()))
            self.assertEqual(
                state.list_workspace_mappings()[0].workspace_id,
                workspace_id,
            )

    def test_multiple_source_workspaces_restore_to_different_target_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_modern_codex_home(
                root,
                "TestComputerA",
                provider="source-provider",
                model="gpt-source",
                populated=True,
            )
            source_alpha = root / "Source" / "Alpha"
            source_beta = root / "Source" / "Beta"
            source_alpha.mkdir(parents=True)
            source_beta.mkdir(parents=True)
            with closing(sqlite3.connect(source / "state_5.sqlite")) as conn:
                conn.execute(
                    "UPDATE threads SET cwd = ? WHERE id LIKE '1111%'",
                    (str(source_alpha),),
                )
                conn.execute(
                    "UPDATE threads SET cwd = ? WHERE id LIKE '2222%'",
                    (str(source_beta),),
                )
                conn.commit()

            source_state = SyncStateStore(
                make_paths(root / "source-state"),
                XorProtector(),
            )
            workspace_repository = MemoryWorkspaceRepository()
            source_manager = WorkspaceManager(
                FakeAuth(),
                FakeDevices(),
                source_state,
                workspace_repository,
            )
            repository = MemoryCloudRepository()
            ManualUploadEngine(
                resolve_paths(str(source)),
                FakeAuth(),
                FakeDevices(),
                repository,
                source_manager,
            ).upload()

            active_workspace_id = workspace_id_for_path(USER_ID, str(source_alpha))
            archived_workspace_id = workspace_id_for_path(USER_ID, str(source_beta))
            self.assertEqual(
                repository.threads["11111111-1111-4111-8111-111111111111"][
                    "workspace_id"
                ],
                active_workspace_id,
            )
            self.assertEqual(
                repository.threads["22222222-2222-4222-8222-222222222222"][
                    "workspace_id"
                ],
                archived_workspace_id,
            )

            target = create_modern_codex_home(
                root,
                "TestComputerB",
                provider="target-provider",
                model="gpt-target",
                populated=False,
            )
            target_alpha = root / "Target" / "Alpha"
            target_beta = root / "Target" / "Beta"
            target_alpha.mkdir(parents=True)
            target_beta.mkdir(parents=True)
            target_state = SyncStateStore(
                make_paths(root / "target-state"),
                XorProtector(),
            )
            target_state.save_workspace_mapping(
                active_workspace_id,
                target_alpha,
                "Alpha",
            )
            target_state.save_workspace_mapping(
                archived_workspace_id,
                target_beta,
                "Beta",
            )
            target_manager = WorkspaceManager(
                FakeAuth(),
                FakeDevices(),
                target_state,
                workspace_repository,
            )

            summary = ManualDownloadEngine(
                resolve_paths(str(target)),
                FakeAuth(),
                FakeDevices(),
                repository,
                target_manager,
            ).restore()

            self.assertEqual(summary.local_restore.inserted_threads, 2)
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                rows = conn.execute(
                    "SELECT id, cwd FROM threads ORDER BY id"
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    (
                        "11111111-1111-4111-8111-111111111111",
                        str(target_alpha.resolve()),
                    ),
                    (
                        "22222222-2222-4222-8222-222222222222",
                        str(target_beta.resolve()),
                    ),
                ],
            )

    def test_unmapped_workspace_fails_before_local_restore_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_modern_codex_home(
                root,
                "TestComputerA",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            source_state = SyncStateStore(make_paths(root / "source-state"), XorProtector())
            workspace_repository = MemoryWorkspaceRepository()
            repository = MemoryCloudRepository()
            ManualUploadEngine(
                resolve_paths(str(source)),
                FakeAuth(),
                FakeDevices(),
                repository,
                WorkspaceManager(
                    FakeAuth(),
                    FakeDevices(),
                    source_state,
                    workspace_repository,
                ),
            ).upload()
            target = create_modern_codex_home(
                root,
                "TestComputerB",
                provider="openai",
                model="gpt-target",
                populated=False,
            )
            target_state = SyncStateStore(make_paths(root / "target-state"), XorProtector())
            target_manager = WorkspaceManager(
                FakeAuth(),
                FakeDevices(),
                target_state,
                workspace_repository,
            )

            with self.assertRaisesRegex(RuntimeError, "not mapped"):
                ManualDownloadEngine(
                    resolve_paths(str(target)),
                    FakeAuth(),
                    FakeDevices(),
                    repository,
                    target_manager,
                ).restore()

            self.assertFalse((target / "history_sync_backups").exists())


class SupabaseWorkspaceRepositoryTests(unittest.TestCase):
    def test_workspace_and_device_mapping_request_contract(self) -> None:
        class RecordingTransport:
            def __init__(self, responses: list[HttpResponse]) -> None:
                self.responses = responses
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
                        "body": json.loads(body.decode("utf-8")) if body else None,
                    }
                )
                return self.responses.pop(0)

        def response(payload: object) -> HttpResponse:
            return HttpResponse(
                status=201,
                body=json.dumps(payload).encode("utf-8"),
                headers={"content-type": "application/json"},
            )

        workspace_id = workspace_id_for_path(USER_ID, r"C:\Source\Shop")
        transport = RecordingTransport(
            [
                response([{"id": workspace_id, "name": "Shop"}]),
                response([{"workspace_id": workspace_id, "local_path": r"C:\Source\Shop"}]),
            ]
        )
        repository = SupabaseWorkspaceRepository(
            SupabaseClient(
                SupabaseConfig("https://example.supabase.co", "sb_publishable_example"),
                transport=transport,
            )
        )

        repository.upsert_workspaces(
            USER_ID,
            [{"id": workspace_id, "name": "Shop"}],
            "access-token",
        )
        repository.upsert_device_workspaces(
            USER_ID,
            DEVICE_ID,
            [{"workspace_id": workspace_id, "local_path": r"C:\Source\Shop"}],
            "access-token",
        )

        self.assertIn("on_conflict=user_id%2Cid", transport.requests[0]["url"])
        self.assertIn("on_conflict=device_id%2Cworkspace_id", transport.requests[1]["url"])
        self.assertEqual(transport.requests[1]["body"][0]["user_id"], USER_ID)
        self.assertEqual(transport.requests[1]["body"][0]["device_id"], DEVICE_ID)
        self.assertNotIn("access-token", json.dumps(transport.requests))


if __name__ == "__main__":
    unittest.main()
