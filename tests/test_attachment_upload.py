from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Mapping
from unittest.mock import patch

from codex_sync.attachments.probe import probe_attachments
from codex_sync.cloud.attachments import SupabaseAttachmentRepository
from codex_sync.cloud.storage import StorageObjectSource
from codex_sync.cloud.supabase_client import HttpResponse, SupabaseClient
from codex_sync.config import SupabaseConfig
from codex_sync.hashing import sha256_bytes
from codex_sync.local.repair_engine import resolve_paths
from codex_sync.models import AuthSession, DeviceIdentity
from codex_sync.sync.upload_attachments import AttachmentUploadEngine
from codex_sync.sync.download_attachments import AttachmentDownloadEngine

THREAD_ID = "thread-attachment-001"
SESSION_ID = "session-attachment-001"
USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEVICE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class FakeAuth:
    def restore_session(self) -> AuthSession:
        return AuthSession.from_payload(
            {
                "access_token": "test-access",
                "refresh_token": "test-refresh",
                "expires_in": 3600,
                "user": {"id": USER_ID, "email": "user@example.com"},
            }
        )


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
        self.backup_calls = 0

    def current_device(self) -> DeviceIdentity:
        return self.device

    def register_current_device(self) -> dict[str, object]:
        return self.device.to_dict()

    def record_successful_backup(self) -> dict[str, object]:
        self.backup_calls += 1
        return self.device.to_dict()


class MemoryAttachmentRepository:
    def __init__(self, *, include_cloud_history: bool = True) -> None:
        self.thread_rows = (
            [{"id": "cloud-thread", "codex_thread_id": THREAD_ID}]
            if include_cloud_history
            else []
        )
        self.session_rows = (
            [
                {
                    "id": "cloud-session",
                    "thread_id": "cloud-thread",
                    "codex_session_id": SESSION_ID,
                    "relative_path": "sessions/2026/08/21/rollout-attachment-fixture.jsonl",
                }
            ]
            if include_cloud_history
            else []
        )
        self.attachments: dict[str, dict[str, object]] = {}
        self.references: dict[tuple[str, str], dict[str, object]] = {}
        self.objects: dict[str, bytes] = {}
        self.upload_calls = 0

    def list_threads(self, user_id, access_token):
        return list(self.thread_rows)

    def list_sessions(self, user_id, access_token):
        return list(self.session_rows)

    def list_attachments(self, user_id, access_token):
        return list(self.attachments.values())

    def list_references(self, user_id, access_token):
        return list(self.references.values())

    def upload_object(self, object_path, content, mime_type, access_token):
        self.upload_calls += 1
        self.objects[object_path] = content

    def upload_source(
        self,
        user_id,
        object_path,
        source: StorageObjectSource,
        mime_type,
        access_token,
    ):
        with source.open() as handle:
            content = handle.read()
        self.upload_object(object_path, content, mime_type, access_token)
        return object_path

    def download_object(self, object_path, access_token):
        return self.objects[object_path]

    def upsert_attachments(self, user_id, device_id, rows, access_token):
        mapping = {}
        for row in rows:
            digest = str(row["sha256"])
            stored = {
                **row,
                "id": self.attachments.get(digest, {}).get(
                    "id",
                    f"attachment-{len(self.attachments) + 1}",
                ),
                "user_id": user_id,
                "source_device_id": device_id,
            }
            self.attachments[digest] = stored
            mapping[digest] = str(stored["id"])
        return mapping

    def upsert_references(self, user_id, rows, access_token):
        output = []
        for row in rows:
            key = (str(row["attachment_id"]), str(row["reference_location"]))
            stored = {
                **row,
                "id": self.references.get(key, {}).get(
                    "id",
                    f"reference-{len(self.references) + 1}",
                ),
                "user_id": user_id,
            }
            self.references[key] = stored
            output.append(stored)
        return output


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


def create_attachment_home(root: Path) -> Path:
    codex_home = root / ".codex"
    codex_home.mkdir(parents=True)
    attachments_dir = codex_home / "attachments"
    text_path = attachments_dir / "text-id" / "pasted-text.txt"
    text_path.parent.mkdir(parents=True)
    text_path.write_text("attachment text", encoding="utf-8")
    pdf_path = attachments_dir / "pdf-id" / "report.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.7\nfixture-pdf\n")
    image_content = b"\x89PNG\r\n\x1a\nfixture-image"
    image_path = codex_home / "temp" / "clipboard.png"
    image_path.parent.mkdir()
    image_path.write_bytes(image_content)
    image_uri = "data:image/png;base64," + base64.b64encode(image_content).decode("ascii")

    manifest = {
        "attachmentPaths": [str(text_path)],
        "pendingRemovalPaths": [str(text_path)],
    }
    (attachments_dir / "pasted-text-attachments.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    session_dir = codex_home / "sessions" / "2026" / "08" / "21"
    session_dir.mkdir(parents=True)
    items = [
        {
            "type": "session_meta",
            "payload": {
                "id": THREAD_ID,
                "session_id": SESSION_ID,
                "model_provider": "openai",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "message-1",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"# Files pasted by the user:\n\n{text_path}",
                    },
                    {"type": "input_image", "image_url": image_uri},
                    {"type": "input_file", "file_path": str(pdf_path)},
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "id": "message-1",
                "local_images": [str(image_path)],
            },
        },
    ]
    (session_dir / "rollout-attachment-fixture.jsonl").write_text(
        "\n".join(json.dumps(item, separators=(",", ":")) for item in items) + "\n",
        encoding="utf-8",
    )
    return codex_home


def create_attachment_restore_target(root: Path, source_home: Path) -> Path:
    target = root / "TestComputerB" / ".codex"
    target.mkdir(parents=True)
    (target / "config.toml").write_text('model_provider = "openai"\n', encoding="utf-8")
    with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL)"
        )
        conn.commit()
    source_session = (
        source_home / "sessions" / "2026" / "08" / "21" / "rollout-attachment-fixture.jsonl"
    )
    target_session = (
        target / "sessions" / "2026" / "08" / "21" / "rollout-attachment-fixture.jsonl"
    )
    target_session.parent.mkdir(parents=True)
    shutil.copyfile(source_session, target_session)
    return target


class AttachmentUploadTests(unittest.TestCase):
    def test_deduplicates_embedded_image_and_uploads_text_pdf_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = create_attachment_home(Path(temp_dir))
            repository = MemoryAttachmentRepository()
            devices = FakeDevices()
            engine = AttachmentUploadEngine(
                resolve_paths(str(codex_home)),
                FakeAuth(),
                devices,
                repository,
            )

            first = engine.upload()
            second = engine.upload()

            self.assertEqual(first.scanned_references, 6)
            self.assertEqual(first.eligible_references, 5)
            self.assertEqual(first.skipped_missing_references, 1)
            self.assertEqual(first.unique_attachments, 3)
            self.assertEqual(first.uploaded_objects, 3)
            self.assertEqual(first.upserted_references, 5)
            self.assertEqual(second.uploaded_objects, 0)
            self.assertEqual(second.reused_objects, 3)
            self.assertEqual(repository.upload_calls, 3)
            self.assertEqual(len(repository.objects), 3)
            self.assertEqual(len(repository.attachments), 3)
            self.assertEqual(len(repository.references), 5)
            self.assertEqual(devices.backup_calls, 2)

            suffixes = {Path(path).suffix for path in repository.objects}
            self.assertEqual(suffixes, {".txt", ".png", ".pdf"})
            for digest, row in repository.attachments.items():
                content = repository.objects[str(row["storage_path"])]
                self.assertEqual(sha256_bytes(content), digest)
                self.assertTrue(str(row["storage_path"]).startswith(f"users/{USER_ID}/attachments/"))

            public_probe = probe_attachments(codex_home).to_dict()
            serialized = json.dumps(public_probe)
            self.assertNotIn("embedded_content", serialized)
            self.assertNotIn(base64.b64encode(b"fixture-image").decode("ascii"), serialized)

    def test_requires_thread_and_session_backup_before_attachment_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = MemoryAttachmentRepository(include_cloud_history=False)
            engine = AttachmentUploadEngine(
                resolve_paths(str(create_attachment_home(Path(temp_dir)))),
                FakeAuth(),
                FakeDevices(),
                repository,
            )

            with self.assertRaisesRegex(RuntimeError, "Thread is not uploaded yet"):
                engine.upload()

            self.assertEqual(repository.upload_calls, 0)
            self.assertEqual(repository.objects, {})

    def test_downloads_attachments_and_rewrites_old_computer_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_attachment_home(root / "TestComputerA")
            repository = MemoryAttachmentRepository()
            AttachmentUploadEngine(
                resolve_paths(str(source)),
                FakeAuth(),
                FakeDevices(),
                repository,
            ).upload()
            target = create_attachment_restore_target(root, source)

            summary = AttachmentDownloadEngine(
                resolve_paths(str(target)),
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore()

            self.assertEqual(summary.downloaded_objects, 3)
            self.assertEqual(summary.local_restore.created_files, 3)
            self.assertEqual(summary.local_restore.verified_files, 3)
            self.assertEqual(summary.local_restore.rewritten_sessions, 1)
            self.assertGreaterEqual(summary.local_restore.rewritten_references, 3)
            restored_probe = probe_attachments(target)
            local_records = [
                item for item in restored_probe.attachments if item.local_path
            ]
            self.assertGreaterEqual(len(local_records), 3)
            self.assertTrue(
                all(str(target / "restored_attachments") in item.local_path for item in local_records)
            )
            self.assertTrue(all(item.exists for item in local_records))
            restored_manifest = target / "attachments" / "pasted-text-attachments.json"
            self.assertTrue(restored_manifest.is_file())
            manifest = json.loads(restored_manifest.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["attachmentPaths"]), 1)
            self.assertTrue(
                all(
                    str(target / "restored_attachments") in value
                    for value in manifest["attachmentPaths"]
                )
            )
            self.assertEqual(
                {item.sha256 for item in restored_probe.attachments if item.sha256},
                set(repository.attachments),
            )
            second = AttachmentDownloadEngine(
                resolve_paths(str(target)),
                FakeAuth(),
                FakeDevices(),
                repository,
            ).restore()
            self.assertEqual(second.downloaded_objects, 0)
            self.assertEqual(second.local_restore.created_files, 0)
            self.assertEqual(second.local_restore.rewritten_sessions, 0)
            self.assertIsNone(second.local_restore.safety_backup)

    def test_attachment_session_write_failure_rolls_back_created_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_attachment_home(root / "TestComputerA")
            repository = MemoryAttachmentRepository()
            AttachmentUploadEngine(
                resolve_paths(str(source)),
                FakeAuth(),
                FakeDevices(),
                repository,
            ).upload()
            target = create_attachment_restore_target(root, source)
            session_path = (
                target / "sessions" / "2026" / "08" / "21"
                / "rollout-attachment-fixture.jsonl"
            )
            original_session = session_path.read_bytes()
            from codex_sync.local import attachment_restore

            failure_injected = False

            real_rewrite = attachment_restore.atomic_rewrite_session_jsonl

            def fail_session_write(path, replacements):
                nonlocal failure_injected
                if path == session_path and not failure_injected:
                    failure_injected = True
                    raise OSError("injected Session write failure")
                return real_rewrite(path, replacements)

            with patch(
                "codex_sync.local.attachment_restore.atomic_rewrite_session_jsonl",
                side_effect=fail_session_write,
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    AttachmentDownloadEngine(
                        resolve_paths(str(target)),
                        FakeAuth(),
                        FakeDevices(),
                        repository,
                    ).restore()

            self.assertEqual(session_path.read_bytes(), original_session)
            restored_files = list((target / "restored_attachments").rglob("*"))
            self.assertFalse(any(path.is_file() for path in restored_files))
            self.assertEqual(
                len(list((target / "history_sync_backups").glob("*.bak"))),
                1,
            )


class SupabaseAttachmentRepositoryTests(unittest.TestCase):
    def test_storage_and_upsert_request_contract(self) -> None:
        content = b"%PDF-1.7\nfixture\n"
        digest = sha256_bytes(content)
        object_path = f"users/{USER_ID}/attachments/{digest[:2]}/{digest}.pdf"
        transport = RecordingTransport(
            [
                json_response(200, {"Key": object_path}),
                json_response(201, [{"id": "attachment-1", "sha256": digest}]),
                json_response(201, [{"id": "reference-1"}]),
            ]
        )
        repository = SupabaseAttachmentRepository(
            SupabaseClient(
                SupabaseConfig(
                    "https://example.supabase.co",
                    "sb_publishable_example",
                ),
                transport=transport,
            )
        )

        repository.upload_object(object_path, content, "application/pdf", "access-token")
        mapping = repository.upsert_attachments(
            USER_ID,
            DEVICE_ID,
            [
                {
                    "sha256": digest,
                    "file_name": "report.pdf",
                    "file_extension": ".pdf",
                    "mime_type": "application/pdf",
                    "file_size": len(content),
                    "storage_path": object_path,
                    "reference_kind": "input_file",
                }
            ],
            "access-token",
        )
        repository.upsert_references(
            USER_ID,
            [
                {
                    "attachment_id": mapping[digest],
                    "reference_kind": "input_file",
                    "reference_location": "sessions/example.jsonl:2",
                }
            ],
            "access-token",
        )

        storage_request = transport.requests[0]
        self.assertEqual(storage_request["body"], content)
        self.assertEqual(storage_request["headers"]["Content-Type"], "application/pdf")
        self.assertEqual(storage_request["headers"]["x-upsert"], "true")
        self.assertIn(object_path, storage_request["url"])
        attachment_request = transport.requests[1]
        self.assertIn("on_conflict=user_id%2Csha256", attachment_request["url"])
        reference_request = transport.requests[2]
        self.assertIn(
            "on_conflict=user_id%2Cattachment_id%2Creference_location",
            reference_request["url"],
        )


if __name__ == "__main__":
    unittest.main()
