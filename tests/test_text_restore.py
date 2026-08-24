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
from codex_sync.hashing import sha256_bytes, sha256_file
from codex_sync.local.repair_engine import (
    get_status,
    read_session_index,
    resolve_paths,
    write_session_index,
)
from codex_sync.local.restore_engine import canonical_session_cwd_matches
from codex_sync.local.thread_diagnose import diagnose_thread
from codex_sync.models import AuthSession, DeviceIdentity
from codex_sync.sync.download import (
    ManualDownloadEngine,
    SessionContentConflictError,
)
from codex_sync.sync.upload import ManualUploadEngine
from unittest.mock import patch

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
                "metadata": {
                    "index_thread_name": item.index_thread_name,
                    "index_updated_at": item.index_updated_at,
                },
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


def cloud_session_row(
    repository: MemoryCloudRepository,
    thread_id: str = ACTIVE_THREAD_ID,
) -> dict[str, object]:
    return next(
        row
        for row in repository.sessions.values()
        if row["thread_id"] == f"cloud-{thread_id}"
    )


def write_conflicting_session(
    target: Path,
    repository: MemoryCloudRepository,
    *,
    thread_id: str = ACTIVE_THREAD_ID,
) -> tuple[Path, bytes, dict[str, object]]:
    row = cloud_session_row(repository, thread_id)
    relative_path = str(row["relative_path"])
    session_path = target.joinpath(*Path(relative_path).parts)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "session_id": "local-conflicting-session",
                    "model_provider": "openai",
                    "model": "gpt-source",
                    "cwd": r"C:\Users\ComputerB\Projects\local",
                },
            },
            separators=(",", ":"),
        )
        + "\n"
        + '{"type":"event_msg","payload":{"message":"local version"}}\n'
    ).encode("utf-8")
    session_path.write_bytes(content)
    return session_path, content, row


class TextRestoreTests(unittest.TestCase):
    def test_ordinary_restore_does_not_overwrite_existing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_modern_codex_home(
                root,
                "TestComputerA",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            write_session_index(
                resolve_paths(str(source)),
                [
                    {
                        "id": ACTIVE_THREAD_ID,
                        "thread_name": "Cloud index title",
                        "updated_at": "2026-08-21T12:34:56Z",
                    }
                ],
            )
            repository = upload_test_computer_a(source)
            target = create_modern_codex_home(
                root,
                "TestComputerB",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            paths = resolve_paths(str(target))
            write_session_index(
                paths,
                [
                    {
                        "id": ACTIVE_THREAD_ID,
                        "thread_name": "Local index title",
                        "updated_at": "2026-08-22T00:00:00Z",
                    }
                ],
            )
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                conn.execute(
                    """
                    UPDATE threads
                    SET title = 'Local DB title',
                        source = 'local-source',
                        cwd = 'C:\\LocalOnly',
                        thread_source = 'local-thread-source',
                        history_mode = 'local-history'
                    WHERE id = ?
                    """,
                    (ACTIVE_THREAD_ID,),
                )
                conn.commit()

            summary = ManualDownloadEngine(
                paths,
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore(codex_thread_id=ACTIVE_THREAD_ID, target_cwd=root)

            self.assertEqual(summary.local_restore.reconciled_threads, 0)
            self.assertIsNone(summary.local_restore.safety_backup)
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                row = conn.execute(
                    """
                    SELECT title, source, cwd, thread_source, history_mode
                    FROM threads WHERE id = ?
                    """,
                    (ACTIVE_THREAD_ID,),
                ).fetchone()
            self.assertEqual(
                row,
                (
                    "Local DB title",
                    "local-source",
                    r"C:\LocalOnly",
                    "local-thread-source",
                    "local-history",
                ),
            )
            self.assertEqual(
                read_session_index(paths)[ACTIVE_THREAD_ID]["thread_name"],
                "Local index title",
            )

    def test_explicit_reconcile_repairs_existing_metadata_and_selected_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_modern_codex_home(
                root,
                "TestComputerA",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            desired_index_name = "Codex 通用名 API 管理中心==最终完整部署任务……"
            desired_index_updated = "2026-08-21T12:36:44Z"
            write_session_index(
                resolve_paths(str(source)),
                [
                    {
                        "id": ACTIVE_THREAD_ID,
                        "thread_name": desired_index_name,
                        "updated_at": desired_index_updated,
                    }
                ],
            )
            repository = upload_test_computer_a(source)
            target = create_modern_codex_home(
                root,
                "TestComputerB",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            paths = resolve_paths(str(target))
            workspace = root / "MappedWorkspace"
            workspace.mkdir()
            local_only_id = "33333333-3333-4333-8333-333333333333"
            local_only_session = (
                target
                / "sessions"
                / "2026"
                / "08"
                / "22"
                / f"rollout-2026-08-22T00-00-00-{local_only_id}.jsonl"
            )
            local_only_session.parent.mkdir(parents=True)
            local_only_session.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": local_only_id,
                            "model_provider": "openai",
                            "model": "gpt-source",
                            "cwd": str(workspace),
                            "source": "desktop",
                            "thread_source": "user",
                            "history_mode": "legacy",
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                conn.execute(
                    """
                    UPDATE threads
                    SET rollout_path = 'C:\\Broken\\missing.jsonl',
                        title = 'Stale DB title',
                        source = 'stale-source',
                        archived = 1,
                        cwd = 'C:\\Stale',
                        created_at = 1,
                        updated_at = 2,
                        created_at_ms = 1000,
                        updated_at_ms = 2000,
                        thread_source = 'stale-thread-source',
                        history_mode = 'stale-history'
                    WHERE id = ?
                    """,
                    (ACTIVE_THREAD_ID,),
                )
                conn.execute(
                    """
                    UPDATE threads SET archived = 0, cwd = 'C:\\Stale'
                    WHERE id = ?
                    """,
                    (ARCHIVED_THREAD_ID,),
                )
                conn.execute(
                    """
                    INSERT INTO threads (
                        id, rollout_path, created_at, updated_at, source,
                        model_provider, cwd, title, sandbox_policy, approval_mode,
                        archived, model, created_at_ms, updated_at_ms,
                        thread_source, history_mode
                    ) VALUES (?, ?, 1700001000, 1700001010, 'desktop', 'openai', ?,
                              'Local-only title', '{"type":"disabled"}', 'never',
                              0, 'gpt-source', 1700001000000, 1700001010000,
                              'user', 'legacy')
                    """,
                    (local_only_id, str(local_only_session), str(workspace)),
                )
                conn.commit()
            local_only_index = {
                "id": local_only_id,
                "thread_name": "Local-only exact index",
                "updated_at": "2026-08-22T01:02:03Z",
            }
            write_session_index(
                paths,
                [
                    {
                        "id": ACTIVE_THREAD_ID,
                        "thread_name": "Stale restored title",
                        "updated_at": "2026-08-20T00:00:00Z",
                    },
                    {
                        "id": ARCHIVED_THREAD_ID,
                        "thread_name": "Incorrect active archived entry",
                        "updated_at": "2026-08-20T00:00:00Z",
                    },
                    local_only_index,
                ],
            )

            first = ManualDownloadEngine(
                paths,
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore(
                target_cwd=workspace,
                reconcile_existing_thread_metadata=True,
            )

            self.assertEqual(first.local_restore.reconciled_threads, 2)
            self.assertEqual(first.local_restore.session_meta_cwd_mismatches, 0)
            self.assertIsNotNone(first.local_restore.safety_backup)
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                row = conn.execute(
                    """
                    SELECT rollout_path, title, source, archived, cwd,
                           created_at, updated_at, created_at_ms, updated_at_ms,
                           model_provider, model, thread_source, history_mode,
                           sandbox_policy, approval_mode
                    FROM threads WHERE id = ?
                    """,
                    (ACTIVE_THREAD_ID,),
                ).fetchone()
                local_only_row = conn.execute(
                    "SELECT title, cwd FROM threads WHERE id = ?",
                    (local_only_id,),
                ).fetchone()
                restored_session_rows = conn.execute(
                    "SELECT id, rollout_path FROM threads WHERE id IN (?, ?)",
                    (ACTIVE_THREAD_ID, ARCHIVED_THREAD_ID),
                ).fetchall()
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(Path(row[0]).parent.name, "21")
            self.assertEqual(row[1:13], (
                "Thread 1",
                "vscode",
                0,
                str(workspace),
                1700000000,
                1700000010,
                1700000000000,
                1700000010000,
                "openai",
                "gpt-source",
                "user",
                "legacy",
            ))
            self.assertEqual(row[13], '{"type":"disabled"}')
            self.assertEqual(row[14], "never")
            self.assertEqual(local_only_row, ("Local-only title", str(workspace)))
            for thread_id, rollout_path in restored_session_rows:
                payload = json.loads(
                    Path(rollout_path).read_text(encoding="utf-8").splitlines()[0]
                )["payload"]
                self.assertEqual(payload["id"], thread_id)
                self.assertEqual(payload["cwd"], str(workspace))
                self.assertEqual(payload["model_provider"], "openai")
                self.assertEqual(payload["model"], "gpt-source")
            self.assertEqual(integrity, "ok")

            index = read_session_index(paths)
            self.assertEqual(index[ACTIVE_THREAD_ID]["thread_name"], desired_index_name)
            self.assertEqual(index[ACTIVE_THREAD_ID]["updated_at"], desired_index_updated)
            self.assertNotIn(ARCHIVED_THREAD_ID, index)
            self.assertEqual(index[local_only_id], local_only_index)

            retry = ManualDownloadEngine(
                paths,
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore(
                target_cwd=workspace,
                reconcile_existing_thread_metadata=True,
            )
            self.assertEqual(retry.local_restore.reconciled_threads, 0)
            self.assertEqual(
                read_session_index(paths)[ACTIVE_THREAD_ID]["thread_name"],
                desired_index_name,
            )

    def test_windows_extended_path_is_canonical_equal_but_different_drive_is_not(self) -> None:
        self.assertTrue(
            canonical_session_cwd_matches(
                r"\\?\C:\Users\ComputerB\Workspace",
                Path(r"C:\Users\ComputerB\Workspace"),
            )
        )
        self.assertFalse(
            canonical_session_cwd_matches(
                r"F:\CodexProjects\Workspace",
                Path(r"C:\Users\ComputerB\Workspace"),
            )
        )

    def test_legacy_cloud_without_index_metadata_preserves_valid_local_name(self) -> None:
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
            repository.threads[ACTIVE_THREAD_ID]["metadata"] = {}
            target = create_modern_codex_home(
                root,
                "TestComputerB",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            paths = resolve_paths(str(target))
            write_session_index(
                paths,
                [
                    {
                        "id": ACTIVE_THREAD_ID,
                        "thread_name": "Valid legacy local index",
                        "updated_at": "2026-08-20T00:00:00Z",
                    }
                ],
            )

            ManualDownloadEngine(
                paths,
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore(
                codex_thread_id=ACTIVE_THREAD_ID,
                target_cwd=root,
                reconcile_existing_thread_metadata=True,
            )

            self.assertEqual(
                read_session_index(paths)[ACTIVE_THREAD_ID]["thread_name"],
                "Valid legacy local index",
            )

    def test_cloud_null_index_metadata_preserves_valid_local_name(self) -> None:
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
            repository.threads[ACTIVE_THREAD_ID]["metadata"] = {
                "index_thread_name": None,
                "index_updated_at": None,
            }
            target = create_modern_codex_home(
                root,
                "TestComputerB",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            paths = resolve_paths(str(target))
            write_session_index(
                paths,
                [
                    {
                        "id": ACTIVE_THREAD_ID,
                        "thread_name": "Valid null-metadata local index",
                        "updated_at": "2026-08-20T00:00:00Z",
                    }
                ],
            )

            ManualDownloadEngine(
                paths,
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore(
                codex_thread_id=ACTIVE_THREAD_ID,
                target_cwd=root,
                reconcile_existing_thread_metadata=True,
            )

            self.assertEqual(
                read_session_index(paths)[ACTIVE_THREAD_ID]["thread_name"],
                "Valid null-metadata local index",
            )

    def test_thread_diagnose_reads_only_requested_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = create_modern_codex_home(
                Path(temp_dir),
                "DiagnosticComputer",
                provider="openai",
                model="gpt-source",
                populated=True,
            )
            paths = resolve_paths(str(codex_home))
            write_session_index(
                paths,
                [
                    {
                        "id": ACTIVE_THREAD_ID,
                        "thread_name": "Diagnostic index",
                        "updated_at": "2026-08-21T00:00:00Z",
                    }
                ],
            )

            payload = diagnose_thread(paths, ACTIVE_THREAD_ID)

            self.assertEqual(payload["thread_id"], ACTIVE_THREAD_ID)
            self.assertEqual(payload["database"]["title"], "Thread 1")
            self.assertEqual(payload["session_index"]["thread_name"], "Diagnostic index")
            self.assertEqual(payload["session_meta"]["id"], ACTIVE_THREAD_ID)
            self.assertEqual(payload["session_meta"]["source"], "vscode")
            self.assertNotIn("content", payload["session_meta"])

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
            self.assertEqual(second.downloaded_session_objects, 2)
            self.assertEqual(second.reused_local_sessions, 2)
            self.assertEqual(second.local_restore.inserted_threads, 0)
            self.assertEqual(second.local_restore.created_sessions, 0)
            self.assertIsNone(second.local_restore.safety_backup)
            self.assertEqual(repository.download_calls, 4)

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

    def test_default_restore_blocks_same_path_session_content_conflict(self) -> None:
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
                provider="target-provider",
                model="gpt-target",
                populated=False,
            )
            session_path, original_content, row = write_conflicting_session(
                target,
                repository,
            )

            with self.assertRaises(SessionContentConflictError) as raised:
                ManualDownloadEngine(
                    resolve_paths(str(target)),
                    FakeAuth(),
                    FakeDevices(),
                    repository,
                ).restore(target_cwd=root)

            conflict = raised.exception.conflict
            self.assertEqual(conflict.relative_path, str(row["relative_path"]))
            self.assertEqual(conflict.codex_session_id, str(row["codex_session_id"]))
            self.assertEqual(conflict.local_sha256, sha256_bytes(original_content))
            self.assertEqual(conflict.cloud_sha256, str(row["content_hash"]))
            self.assertEqual(conflict.local_file_size, len(original_content))
            self.assertEqual(conflict.cloud_file_size, int(row["file_size"]))
            self.assertIn("local_sha256=", str(raised.exception))
            self.assertIn("cloud_sha256=", str(raised.exception))
            self.assertEqual(session_path.read_bytes(), original_content)
            self.assertEqual(repository.download_calls, 1)
            self.assertFalse((target / "history_sync_backups").exists())
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 0)

    def test_cloud_wins_replaces_conflict_atomically_and_adapts_session_metadata(self) -> None:
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
                provider="target-provider",
                model="gpt-target",
                populated=False,
            )
            session_path, original_content, row = write_conflicting_session(
                target,
                repository,
            )

            summary = ManualDownloadEngine(
                resolve_paths(str(target)),
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore(
                target_cwd=root,
                replace_conflicting_sessions=True,
            )

            cloud_content = repository.objects[str(row["storage_path"])]
            self.assertEqual(summary.replaced_conflicting_sessions, 1)
            self.assertEqual(summary.downloaded_session_objects, 2)
            self.assertEqual(summary.local_restore.existing_sessions, 1)
            self.assertEqual(summary.local_restore.inserted_threads, 2)
            actual_lines = session_path.read_bytes().splitlines()
            cloud_lines = cloud_content.splitlines()
            self.assertEqual(actual_lines[1:], cloud_lines[1:])
            payload = json.loads(actual_lines[0])["payload"]
            self.assertEqual(payload["cwd"], str(root.resolve()))
            self.assertEqual(payload["model_provider"], "target-provider")
            self.assertEqual(payload["model"], "gpt-target")
            self.assertNotEqual(sha256_file(session_path), str(row["content_hash"]))
            self.assertNotEqual(session_path.read_bytes(), original_content)
            self.assertEqual(repository.download_calls, 2)
            self.assertIn(ACTIVE_THREAD_ID, read_session_index(resolve_paths(str(target))))

    def test_conflict_download_or_hash_failure_preserves_local_session(self) -> None:
        for failure_kind in ("download", "hash"):
            with self.subTest(failure_kind=failure_kind):
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
                        model="gpt-source",
                        populated=False,
                    )
                    session_path, original_content, row = write_conflicting_session(
                        target,
                        repository,
                    )
                    if failure_kind == "download":
                        repository.download_session = lambda *_args: (_ for _ in ()).throw(
                            OSError("injected Session download failure")
                        )
                        expected_error = "injected Session download failure"
                    else:
                        storage_path = str(row["storage_path"])
                        cloud_content = repository.objects[storage_path]
                        repository.objects[storage_path] = (
                            bytes([cloud_content[0] ^ 1]) + cloud_content[1:]
                        )
                        expected_error = "SHA256 mismatch"

                    engine = ManualDownloadEngine(
                        resolve_paths(str(target)),
                        FakeAuth(),
                        FakeDevices(),
                        repository,
                    )
                    if failure_kind == "download":
                        with self.assertRaisesRegex(OSError, expected_error):
                            engine.restore(
                                target_cwd=root,
                                replace_conflicting_sessions=True,
                            )
                    else:
                        with self.assertRaisesRegex(RuntimeError, expected_error):
                            engine.restore(
                                target_cwd=root,
                                replace_conflicting_sessions=True,
                            )

                    self.assertEqual(session_path.read_bytes(), original_content)
                    with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                        self.assertEqual(
                            conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0],
                            0,
                        )

    def test_atomic_replace_failure_preserves_local_session_and_retry_succeeds(self) -> None:
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
                model="gpt-source",
                populated=False,
            )
            session_path, original_content, _row = write_conflicting_session(
                target,
                repository,
            )
            from codex_sync.local import restore_engine

            real_atomic_adapt = restore_engine.atomic_adapt_session_file
            failure_injected = False

            def fail_once(
                source_path: Path,
                destination: Path,
                *_args: object,
                **_kwargs: object,
            ) -> dict[str, object]:
                nonlocal failure_injected
                if destination == session_path and not failure_injected:
                    failure_injected = True
                    raise OSError("injected atomic replacement failure")
                return real_atomic_adapt(source_path, destination, *_args, **_kwargs)

            with patch(
                "codex_sync.local.restore_engine.atomic_adapt_session_file",
                side_effect=fail_once,
            ):
                with self.assertRaisesRegex(OSError, "injected atomic replacement failure"):
                    ManualDownloadEngine(
                        resolve_paths(str(target)),
                        FakeAuth(),
                        FakeDevices(),
                        repository,
                    ).restore(
                        target_cwd=root,
                        replace_conflicting_sessions=True,
                    )

            self.assertEqual(session_path.read_bytes(), original_content)
            with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 0)

            retry = ManualDownloadEngine(
                resolve_paths(str(target)),
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore(
                target_cwd=root,
                replace_conflicting_sessions=True,
            )
            self.assertEqual(retry.replaced_conflicting_sessions, 1)
            self.assertEqual(retry.local_restore.verified_sessions, 2)
            self.assertNotEqual(session_path.read_bytes(), original_content)

            rerun = ManualDownloadEngine(
                resolve_paths(str(target)),
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore(target_cwd=root)
            self.assertEqual(rerun.reused_local_sessions, 2)
            self.assertEqual(rerun.downloaded_session_objects, 2)

    def test_cloud_wins_handles_a_52mb_session_conflict(self) -> None:
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
            row = cloud_session_row(repository)
            target_size = 52 * 1024 * 1024
            first_line = (
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": ACTIVE_THREAD_ID,
                            "session_id": "large-session",
                            "model_provider": "openai",
                            "model": "gpt-source",
                            "cwd": r"C:\Users\ComputerA\Projects\history",
                        },
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            prefix = b'{"type":"event_msg","payload":{"message":"'
            suffix = b'"}}\n'
            fill_size = target_size - len(first_line) - len(prefix) - len(suffix)
            self.assertGreater(fill_size, 0)
            cloud_content = first_line + prefix + (b"x" * fill_size) + suffix
            digest = sha256_bytes(cloud_content)
            old_storage_path = str(row["storage_path"])
            repository.objects.pop(old_storage_path)
            storage_path = f"users/{USER_ID}/sessions/{digest}.jsonl"
            repository.objects[storage_path] = cloud_content
            row["content_hash"] = digest
            row["file_size"] = len(cloud_content)
            row["storage_path"] = storage_path

            target = create_modern_codex_home(
                root,
                "TestComputerB",
                provider="openai",
                model="gpt-source",
                populated=False,
            )
            session_path, _original_content, _ = write_conflicting_session(
                target,
                repository,
            )

            summary = ManualDownloadEngine(
                resolve_paths(str(target)),
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore(
                target_cwd=root,
                replace_conflicting_sessions=True,
            )

            self.assertEqual(summary.replaced_conflicting_sessions, 1)
            actual_content = session_path.read_bytes()
            self.assertEqual(
                actual_content.split(b"\n", 1)[1:],
                cloud_content.split(b"\n", 1)[1:],
            )
            self.assertNotEqual(len(actual_content), target_size)
            self.assertNotEqual(sha256_file(session_path), digest)
            payload = json.loads(actual_content.splitlines()[0])["payload"]
            self.assertEqual(payload["cwd"], str(root.resolve()))

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
