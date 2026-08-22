from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Mapping

from codex_sync.cloud.restore import SupabaseRestoreRepository
from codex_sync.cloud.supabase_client import HttpResponse, SupabaseClient
from codex_sync.config import SupabaseConfig
from codex_sync.hashing import sha256_bytes
from codex_sync.local.repair_engine import get_status, read_session_index, resolve_paths
from codex_sync.models import AuthSession, DeviceIdentity
from codex_sync.sync.download import ManualDownloadEngine
from codex_sync.sync.upload import ManualUploadEngine

ACTIVE_THREAD_ID = "11111111-1111-4111-8111-111111111111"
ARCHIVED_THREAD_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEVICE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class FakeAuth:
    def __init__(self) -> None:
        self.session = AuthSession.from_payload(
            {
                "access_token": "test-access",
                "refresh_token": "test-refresh",
                "expires_in": 3600,
                "user": {"id": USER_ID, "email": "user@example.com"},
            }
        )

    def restore_session(self) -> AuthSession:
        return self.session


class FakeDevices:
    def __init__(self) -> None:
        self.device = DeviceIdentity(
            id=DEVICE_ID,
            device_name="Test Device",
            os_name="Windows",
            os_version="10",
            client_version="2.0.0.dev0",
            first_registered_at="2026-08-21T00:00:00+00:00",
        )

    def current_device(self) -> DeviceIdentity:
        return self.device

    def register_current_device(self) -> dict[str, object]:
        return self.device.to_dict()

    def record_successful_backup(self) -> dict[str, object]:
        return self.device.to_dict()


class MemoryCloudRepository:
    def __init__(self) -> None:
        self.threads: dict[str, dict[str, object]] = {}
        self.sessions: dict[tuple[str, str], dict[str, object]] = {}
        self.objects: dict[str, bytes] = {}
        self.download_calls = 0

    def upsert_threads(
        self,
        user_id,
        device_id,
        threads,
        access_token,
        workspace_ids=None,
    ):
        mapping = {}
        workspace_ids = workspace_ids or {}
        for item in threads:
            cloud_id = f"cloud-{item.codex_thread_id}"
            mapping[item.codex_thread_id] = cloud_id
            self.threads[item.codex_thread_id] = {
                "id": cloud_id,
                "user_id": user_id,
                "codex_thread_id": item.codex_thread_id,
                "workspace_id": workspace_ids.get(item.codex_thread_id),
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
        return mapping

    def list_threads(
        self,
        user_id,
        access_token,
        codex_thread_id=None,
        workspace_id=None,
    ):
        rows = list(self.threads.values())
        if codex_thread_id:
            rows = [row for row in rows if row["codex_thread_id"] == codex_thread_id]
        if workspace_id:
            rows = [row for row in rows if row.get("workspace_id") == workspace_id]
        return rows

    def list_sessions(self, user_id, access_token):
        return list(self.sessions.values())

    def upload_session_object(self, user_id, stable_file, access_token):
        storage_path = f"users/{user_id}/sessions/{stable_file.sha256}.jsonl"
        self.objects[storage_path] = (
            stable_file.content
            if stable_file.content is not None
            else stable_file.path.read_bytes()
        )
        return storage_path

    def upsert_sessions(self, user_id, device_id, rows, access_token):
        output = []
        for row in rows:
            stored = {
                **row,
                "id": f"cloud-session-{len(self.sessions) + 1}",
                "user_id": user_id,
                "updated_at": "2026-08-21T00:00:00+00:00",
            }
            self.sessions[(str(row["thread_id"]), str(row["relative_path"]))] = stored
            output.append(stored)
        return output

    def download_session(self, storage_path, access_token):
        self.download_calls += 1
        return self.objects[storage_path]


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


def create_modern_codex_home(
    root: Path,
    name: str,
    *,
    provider: str,
    model: str,
    populated: bool,
) -> Path:
    codex_home = root / name / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        f'model_provider = "{provider}"\nmodel = "{model}"\n',
        encoding="utf-8",
    )
    db_path = codex_home / "state_5.sqlite"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                sandbox_policy TEXT NOT NULL,
                approval_mode TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                model TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER,
                thread_source TEXT,
                history_mode TEXT NOT NULL DEFAULT 'legacy'
            )
            """
        )
        if populated:
            old_cwd = r"C:\Users\ComputerA\Projects\history"
            for index, (thread_id, archived) in enumerate(
                ((ACTIVE_THREAD_ID, False), (ARCHIVED_THREAD_ID, True))
            ):
                if archived:
                    session_dir = codex_home / "archived_sessions"
                else:
                    session_dir = codex_home / "sessions" / "2026" / "08" / "21"
                session_dir.mkdir(parents=True, exist_ok=True)
                session_path = (
                    session_dir
                    / f"rollout-2026-08-21T00-00-0{index}-{thread_id}.jsonl"
                )
                session_meta = {
                    "timestamp": "2026-08-21T00:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "session_id": f"session-{index + 1}",
                        "model_provider": provider,
                        "model": model,
                        "cwd": old_cwd,
                        "source": "vscode",
                        "thread_source": "user",
                        "history_mode": "legacy",
                    },
                }
                session_path.write_text(
                    json.dumps(session_meta, separators=(",", ":"))
                    + f'\n{{"type":"event_msg","payload":{{"message":"turn-{index}"}}}}\n',
                    encoding="utf-8",
                )
                conn.execute(
                    """
                    INSERT INTO threads (
                        id, rollout_path, created_at, updated_at, source,
                        model_provider, cwd, title, sandbox_policy, approval_mode,
                        archived, model, created_at_ms, updated_at_ms,
                        thread_source, history_mode
                    ) VALUES (?, ?, 1700000000, 1700000010, 'vscode', ?, ?, ?, ?, 'never',
                              ?, ?, 1700000000000, 1700000010000, 'user', 'legacy')
                    """,
                    (
                        thread_id,
                        str(session_path),
                        provider,
                        old_cwd,
                        f"Thread {index + 1}",
                        '{"type":"disabled"}',
                        int(archived),
                        model,
                    ),
                )
        conn.commit()
    return codex_home


def create_legacy_codex_home(root: Path, name: str) -> Path:
    codex_home = root / name / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        'model_provider = "legacy-target"\nmodel = "gpt-legacy-target"\n',
        encoding="utf-8",
    )
    with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn:
        conn.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                model_provider TEXT NOT NULL,
                model TEXT
            )
            """
        )
        conn.commit()
    return codex_home


def upload_test_computer_a(codex_home: Path) -> MemoryCloudRepository:
    repository = MemoryCloudRepository()
    ManualUploadEngine(
        resolve_paths(str(codex_home)),
        FakeAuth(),
        FakeDevices(),
        repository,
    ).upload()
    return repository


class TextRestoreTests(unittest.TestCase):
    def test_computer_a_cloud_computer_b_restore_and_missing_only_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_modern_codex_home(
                root,
                "TestComputerA",
                provider="source-provider",
                model="gpt-source",
                populated=True,
            )
            repository = upload_test_computer_a(source)
            target = create_modern_codex_home(
                root,
                "TestComputerB",
                provider="target-provider",
                model="gpt-target",
                populated=False,
            )
            target_workspace = root / "ComputerBUser" / "Workspace"
            target_workspace.mkdir(parents=True)
            engine = ManualDownloadEngine(
                resolve_paths(str(target)),
                FakeAuth(),
                FakeDevices(),
                repository,
            )

            first = engine.restore(target_cwd=target_workspace)
            second = engine.restore(target_cwd=target_workspace)

            self.assertEqual(first.downloaded_session_objects, 2)
            self.assertEqual(first.local_restore.inserted_threads, 2)
            self.assertEqual(first.local_restore.created_sessions, 2)
            self.assertEqual(first.local_restore.verified_threads, 2)
            self.assertIsNotNone(first.local_restore.safety_backup)
            self.assertTrue(Path(first.local_restore.safety_backup).is_file())
            self.assertEqual(second.downloaded_session_objects, 0)
            self.assertEqual(second.reused_local_sessions, 2)
            self.assertEqual(second.local_restore.inserted_threads, 0)
            self.assertEqual(second.local_restore.created_sessions, 0)
            self.assertIsNone(second.local_restore.safety_backup)
            self.assertEqual(repository.download_calls, 2)

            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                rows = conn.execute(
                    """
                    SELECT id, rollout_path, model_provider, model, cwd, archived
                    FROM threads ORDER BY id
                    """
                ).fetchall()
            self.assertEqual(len(rows), 2)
            for thread_id, rollout_path, provider, model, cwd, _ in rows:
                self.assertIn(str(target), rollout_path)
                self.assertNotIn("ComputerA", rollout_path)
                self.assertEqual(provider, "target-provider")
                self.assertEqual(model, "gpt-target")
                self.assertEqual(cwd, str(target_workspace))
                payload = json.loads(Path(rollout_path).read_text(encoding="utf-8").splitlines()[0])[
                    "payload"
                ]
                self.assertEqual(payload["id"], thread_id)
                self.assertEqual(payload["model_provider"], "target-provider")
                self.assertEqual(payload["model"], "gpt-target")
                self.assertEqual(payload["cwd"], str(target_workspace))

            index = read_session_index(resolve_paths(str(target)))
            self.assertIn(ACTIVE_THREAD_ID, index)
            self.assertNotIn(ARCHIVED_THREAD_ID, index)
            status = get_status(resolve_paths(str(target)))
            self.assertEqual(status["total_threads"], 2)
            self.assertEqual(status["movable_threads"], 0)

    def test_restore_only_one_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_modern_codex_home(
                root,
                "TestComputerA",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            repository = upload_test_computer_a(source)
            target = create_modern_codex_home(
                root,
                "TestComputerB",
                provider="openai",
                model="gpt-target",
                populated=False,
            )

            summary = ManualDownloadEngine(
                resolve_paths(str(target)),
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore(codex_thread_id=ACTIVE_THREAD_ID, target_cwd=root)

            self.assertEqual(summary.cloud_threads, 1)
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                ids = [row[0] for row in conn.execute("SELECT id FROM threads")]
            self.assertEqual(ids, [ACTIVE_THREAD_ID])

    def test_restore_supports_legacy_threads_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_modern_codex_home(
                root,
                "TestComputerA",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            repository = upload_test_computer_a(source)
            target = create_legacy_codex_home(root, "LegacyComputerB")

            summary = ManualDownloadEngine(
                resolve_paths(str(target)),
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore(codex_thread_id=ACTIVE_THREAD_ID, target_cwd=root)

            self.assertEqual(summary.local_restore.inserted_threads, 1)
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                row = conn.execute(
                    "SELECT id, model_provider, model FROM threads"
                ).fetchone()
            self.assertEqual(
                row,
                (ACTIVE_THREAD_ID, "legacy-target", "gpt-legacy-target"),
            )
            self.assertIn(ACTIVE_THREAD_ID, read_session_index(resolve_paths(str(target))))

    def test_corrupt_cloud_object_aborts_before_local_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_modern_codex_home(
                root,
                "TestComputerA",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            repository = upload_test_computer_a(source)
            first_path = next(iter(repository.objects))
            original = repository.objects[first_path]
            repository.objects[first_path] = bytes([original[0] ^ 1]) + original[1:]
            target = create_modern_codex_home(
                root,
                "TestComputerB",
                provider="openai",
                model="gpt-target",
                populated=False,
            )

            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                ManualDownloadEngine(
                    resolve_paths(str(target)),
                    FakeAuth(),
                    FakeDevices(),
                    repository,
                ).restore(target_cwd=root)

            self.assertFalse((target / "history_sync_backups").exists())
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 0)

    def test_database_failure_rolls_back_created_session_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_modern_codex_home(
                root,
                "TestComputerA",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            repository = upload_test_computer_a(source)
            target = create_modern_codex_home(
                root,
                "TestComputerB",
                provider="openai",
                model="gpt-target",
                populated=False,
            )
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                conn.execute(
                    """
                    CREATE TRIGGER reject_cloud_restore
                    BEFORE INSERT ON threads
                    BEGIN
                      SELECT RAISE(ABORT, 'blocked restore');
                    END
                    """
                )
                conn.commit()

            with self.assertRaisesRegex(sqlite3.IntegrityError, "blocked restore"):
                ManualDownloadEngine(
                    resolve_paths(str(target)),
                    FakeAuth(),
                    FakeDevices(),
                    repository,
                ).restore(codex_thread_id=ACTIVE_THREAD_ID, target_cwd=root)

            session_row = next(
                row
                for row in repository.sessions.values()
                if row["thread_id"] == f"cloud-{ACTIVE_THREAD_ID}"
            )
            restored_path = target.joinpath(*Path(str(session_row["relative_path"])).parts)
            self.assertFalse(restored_path.exists())
            self.assertFalse((target / "session_index.jsonl").exists())
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                        "AND name='reject_cloud_restore'"
                    ).fetchone()[0],
                    1,
                )
            backups = list((target / "history_sync_backups").glob("*.bak"))
            self.assertEqual(len(backups), 1)

    def test_memory_cloud_hashes_match_uploaded_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = create_modern_codex_home(
                Path(temp_dir),
                "TestComputerA",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            repository = upload_test_computer_a(source)

            for row in repository.sessions.values():
                content = repository.objects[str(row["storage_path"])]
                self.assertEqual(sha256_bytes(content), row["content_hash"])


class SupabaseRestoreRepositoryTests(unittest.TestCase):
    def test_manifest_and_storage_download_request_contract(self) -> None:
        content = (
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": ACTIVE_THREAD_ID, "model_provider": "openai"},
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        digest = sha256_bytes(content)
        storage_path = f"users/{USER_ID}/sessions/{digest}.jsonl"
        transport = RecordingTransport(
            [
                json_response(
                    200,
                    [
                        {
                            "id": f"cloud-{ACTIVE_THREAD_ID}",
                            "codex_thread_id": ACTIVE_THREAD_ID,
                        }
                    ],
                ),
                json_response(
                    200,
                    [
                        {
                            "thread_id": f"cloud-{ACTIVE_THREAD_ID}",
                            "relative_path": f"sessions/{ACTIVE_THREAD_ID}.jsonl",
                            "content_hash": digest,
                            "file_size": len(content),
                            "storage_path": storage_path,
                        }
                    ],
                ),
                HttpResponse(
                    status=200,
                    body=content,
                    headers={"content-type": "application/x-ndjson"},
                ),
            ]
        )
        repository = SupabaseRestoreRepository(
            SupabaseClient(
                SupabaseConfig(
                    "https://example.supabase.co",
                    "sb_publishable_example",
                ),
                transport=transport,
            )
        )

        threads = repository.list_threads(USER_ID, "access-token", ACTIVE_THREAD_ID)
        sessions = repository.list_sessions(USER_ID, "access-token")
        downloaded = repository.download_session(storage_path, "access-token")

        self.assertEqual(len(threads), 1)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(downloaded, content)
        self.assertIn(f"codex_thread_id=eq.{ACTIVE_THREAD_ID}", transport.requests[0]["url"])
        self.assertIn("user_id=eq.", transport.requests[1]["url"])
        self.assertIn(
            f"/storage/v1/object/codex-history-sync/{storage_path}",
            transport.requests[2]["url"],
        )
        self.assertEqual(transport.requests[2]["headers"]["Authorization"], "Bearer access-token")
        self.assertIsNone(transport.requests[2]["body"])


if __name__ == "__main__":
    unittest.main()
