from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Mapping

from codex_sync.cloud.backup import SupabaseManualUploadRepository
from codex_sync.cloud.supabase_client import HttpResponse, SupabaseClient
from codex_sync.config import SupabaseConfig
from codex_sync.local.catalog import read_stable_file, scan_local_catalog
from codex_sync.local.repair_engine import resolve_paths
from codex_sync.models import AuthSession, DeviceIdentity
from codex_sync.sync.upload import ManualUploadEngine


class FakeAuth:
    def __init__(self, session: AuthSession):
        self.session = session

    def restore_session(self) -> AuthSession:
        return self.session


class FakeDevices:
    def __init__(self, device: DeviceIdentity):
        self.device = device
        self.register_calls = 0
        self.backup_calls = 0

    def current_device(self) -> DeviceIdentity:
        return self.device

    def register_current_device(self) -> dict[str, object]:
        self.register_calls += 1
        return self.device.to_dict()

    def record_successful_backup(self) -> dict[str, object]:
        self.backup_calls += 1
        return self.device.to_dict()


class FakeUploadRepository:
    def __init__(self) -> None:
        self.thread_ids: dict[str, str] = {}
        self.sessions: dict[tuple[str, str], dict[str, object]] = {}
        self.uploaded_objects: list[tuple[str, str, bytes]] = []

    def upsert_threads(self, user_id, device_id, threads, access_token):
        for thread in threads:
            self.thread_ids.setdefault(thread.codex_thread_id, f"cloud-{thread.codex_thread_id}")
        return dict(self.thread_ids)

    def list_sessions(self, user_id, access_token):
        return list(self.sessions.values())

    def upload_session_object(self, user_id, stable_file, access_token):
        path = f"users/{user_id}/sessions/{stable_file.sha256}.jsonl"
        content = (
            stable_file.content
            if stable_file.content is not None
            else stable_file.path.read_bytes()
        )
        self.uploaded_objects.append((stable_file.sha256, path, content))
        return path

    def upsert_sessions(self, user_id, device_id, rows, access_token):
        output = []
        for row in rows:
            stored = dict(row)
            stored["id"] = f"session-row-{len(self.sessions) + 1}"
            stored["user_id"] = user_id
            self.sessions[(str(row["thread_id"]), str(row["relative_path"]))] = stored
            output.append(stored)
        return output


class RecordingTransport:
    def __init__(self, responses: list[HttpResponse]):
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
                "body": body,
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)


def json_response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def create_codex_home(root: Path) -> Path:
    codex_home = root / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "openai"\n', encoding="utf-8")
    session_dir = codex_home / "sessions" / "2026" / "08" / "21"
    session_dir.mkdir(parents=True)
    rows = []
    for index in range(2):
        thread_id = f"thread-{index + 1}"
        path = session_dir / f"rollout-2026-08-21T00-00-0{index}-{thread_id}.jsonl"
        meta = {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "session_id": f"session-{index + 1}",
                "model_provider": "openai",
            },
        }
        path.write_text(json.dumps(meta) + f"\n{{\"turn\":{index}}}\n", encoding="utf-8")
        rows.append((thread_id, str(path), f"Thread {index + 1}", index == 1))

    db_path = codex_home / "state_5.sqlite"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                title TEXT,
                source TEXT,
                model_provider TEXT NOT NULL,
                model TEXT,
                cwd TEXT,
                archived INTEGER NOT NULL,
                created_at INTEGER,
                updated_at INTEGER
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO threads (
                id, rollout_path, title, source, model_provider, model, cwd,
                archived, created_at, updated_at
            ) VALUES (?, ?, ?, 'desktop', 'openai', 'gpt-test', 'C:\\Work', ?, 1700000000, 1700000010)
            """,
            rows,
        )
        conn.commit()
    return codex_home


def auth_session() -> AuthSession:
    return AuthSession.from_payload(
        {
            "access_token": "test-access",
            "refresh_token": "test-refresh",
            "expires_in": 3600,
            "user": {
                "id": "11111111-1111-4111-8111-111111111111",
                "email": "user@example.com",
            },
        }
    )


def device_identity() -> DeviceIdentity:
    return DeviceIdentity(
        id="22222222-2222-4222-8222-222222222222",
        device_name="Test Device",
        os_name="Windows",
        os_version="10",
        client_version="2.0.0.dev0",
        first_registered_at="2026-08-21T00:00:00+00:00",
        last_seen_at="2026-08-21T00:00:00+00:00",
    )


class ManualUploadTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows extended path test")
    def test_catalog_accepts_windows_extended_rollout_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = create_codex_home(Path(temp_dir))
            paths = resolve_paths(str(codex_home))
            original = scan_local_catalog(paths)[0].session.path
            extended = Path("\\\\?\\" + str(original))
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn:
                conn.execute(
                    "UPDATE threads SET rollout_path = ? WHERE id = 'thread-1'",
                    (str(extended),),
                )
                conn.commit()

            records = scan_local_catalog(paths)

            self.assertEqual(records[0].session.relative_path, original.relative_to(codex_home).as_posix())
            self.assertEqual(read_stable_file(records[0].session.path).file_size, original.stat().st_size)

    def test_first_upload_then_unchanged_then_one_changed_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = create_codex_home(Path(temp_dir))
            paths = resolve_paths(str(codex_home))
            repository = FakeUploadRepository()
            devices = FakeDevices(device_identity())
            engine = ManualUploadEngine(paths, FakeAuth(auth_session()), devices, repository)

            first = engine.upload()
            second = engine.upload()
            changed_path = scan_local_catalog(paths)[0].session.path
            changed_path.write_text(changed_path.read_text(encoding="utf-8") + '{"changed":true}\n', encoding="utf-8")
            third = engine.upload()

            self.assertEqual(first.scanned_threads, 2)
            self.assertEqual(first.uploaded_session_objects, 2)
            self.assertEqual(first.upserted_sessions, 2)
            self.assertEqual(first.verified_sessions, 2)
            self.assertEqual(second.uploaded_session_objects, 0)
            self.assertEqual(second.upserted_sessions, 0)
            self.assertEqual(second.unchanged_sessions, 2)
            self.assertEqual(third.uploaded_session_objects, 1)
            self.assertEqual(third.upserted_sessions, 1)
            self.assertEqual(third.unchanged_sessions, 1)
            self.assertEqual(len(repository.uploaded_objects), 3)
            self.assertEqual(devices.register_calls, 3)
            self.assertEqual(devices.backup_calls, 3)

    def test_catalog_rejects_rollout_outside_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = create_codex_home(root)
            outside = root / "outside.jsonl"
            outside.write_text("{}\n", encoding="utf-8")
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn:
                conn.execute(
                    "UPDATE threads SET rollout_path = ? WHERE id = 'thread-1'",
                    (str(outside),),
                )
                conn.commit()

            with self.assertRaisesRegex(RuntimeError, "outside Codex home"):
                scan_local_catalog(resolve_paths(str(codex_home)))


class SupabaseUploadRepositoryTests(unittest.TestCase):
    def test_empty_thread_upload_does_not_call_supabase(self) -> None:
        transport = RecordingTransport([])
        repository = SupabaseManualUploadRepository(
            SupabaseClient(
                SupabaseConfig(
                    "https://example.supabase.co",
                    "sb_publishable_example",
                ),
                transport=transport,
            )
        )

        result = repository.upsert_threads(
            auth_session().user.id,
            device_identity().id,
            [],
            "access-token",
        )

        self.assertEqual(result, {})
        self.assertEqual(transport.requests, [])

    def test_v2_rest_and_storage_request_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = resolve_paths(str(create_codex_home(Path(temp_dir))))
            threads = scan_local_catalog(paths)
            stable = read_stable_file(threads[0].session.path)
            user_id = auth_session().user.id
            device_id = device_identity().id
            transport = RecordingTransport(
                [
                    json_response(
                        201,
                        [
                            {
                                "id": f"cloud-{thread.codex_thread_id}",
                                "codex_thread_id": thread.codex_thread_id,
                            }
                            for thread in threads
                        ],
                    ),
                    json_response(200, []),
                    json_response(200, {"Key": "uploaded"}),
                    json_response(201, [{"id": "cloud-session-1"}]),
                ]
            )
            repository = SupabaseManualUploadRepository(
                SupabaseClient(
                    SupabaseConfig(
                        "https://example.supabase.co",
                        "sb_publishable_example",
                    ),
                    transport=transport,
                )
            )

            mapping = repository.upsert_threads(user_id, device_id, threads, "access-token")
            repository.list_sessions(user_id, "access-token")
            storage_path = repository.upload_session_object(user_id, stable, "access-token")
            repository.upsert_sessions(
                user_id,
                device_id,
                [
                    {
                        "thread_id": mapping[threads[0].codex_thread_id],
                        "codex_session_id": threads[0].session.codex_session_id,
                        "relative_path": threads[0].session.relative_path,
                        "content_hash": stable.sha256,
                        "file_size": stable.file_size,
                        "storage_path": storage_path,
                        "source_mtime_ns": stable.mtime_ns,
                        "last_uploaded_at": "2026-08-21T00:00:00+00:00",
                    }
                ],
                "access-token",
            )

            thread_request = transport.requests[0]
            thread_payload = json.loads(thread_request["body"].decode("utf-8"))
            self.assertIn("on_conflict=user_id%2Ccodex_thread_id", thread_request["url"])
            self.assertNotIn(str(paths.codex_home), json.dumps(thread_payload))

            storage_request = transport.requests[2]
            self.assertIn(
                f"/storage/v1/object/codex-history-sync/users/{user_id}/sessions/{stable.sha256}.jsonl",
                storage_request["url"],
            )
            self.assertEqual(storage_request["headers"]["x-upsert"], "true")
            self.assertEqual(storage_request["body"], stable.content)

            session_request = transport.requests[3]
            session_payload = json.loads(session_request["body"].decode("utf-8"))
            self.assertIn("on_conflict=user_id%2Cthread_id%2Crelative_path", session_request["url"])
            self.assertEqual(session_payload[0]["user_id"], user_id)
            self.assertEqual(session_payload[0]["source_device_id"], device_id)
            self.assertEqual(session_payload[0]["content_hash"], stable.sha256)


if __name__ == "__main__":
    unittest.main()
