from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Mapping

from codex_sync import __version__
from codex_sync.cloud.auth import AuthService
from codex_sync.cloud.devices import DeviceService, SupabaseDeviceRepository
from codex_sync.cloud.supabase_client import HttpResponse, SupabaseClient, SupabaseError
from codex_sync.config import (
    AppPaths,
    SupabaseConfig,
    is_forbidden_client_key,
    load_supabase_config,
    save_supabase_config,
)
from codex_sync.models import AuthSession, UserAccount, utc_now
from codex_sync.security import WindowsDpapiProtector
from codex_sync.sync.state import SyncStateStore


class XorProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return bytes(value ^ 0xA5 for value in plaintext)

    def unprotect(self, protected: bytes) -> bytes:
        return self.protect(protected)


class FakeTransport:
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
                "body": decode_fake_body(body),
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("No fake HTTP response remains")
        return self.responses.pop(0)


def json_response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def decode_fake_body(body: bytes | None) -> object | None:
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body


def make_paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        config_path=root / "config.json",
        state_db_path=root / "sync_state.sqlite",
        logs_dir=root / "logs",
    )


def user_payload(email: str = "user@example.com") -> dict[str, object]:
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "email": email,
        "created_at": "2026-08-21T00:00:00Z",
    }


def session_payload(
    *,
    access_token: str = "access-token-value",
    refresh_token: str = "refresh-token-value",
    expires_in: int = 3600,
) -> dict[str, object]:
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "token_type": "bearer",
        "user": user_payload(),
    }


class CloudConfigTests(unittest.TestCase):
    def test_config_round_trip_and_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = make_paths(Path(temp_dir))
            saved = SupabaseConfig(
                "https://example.supabase.co/",
                "sb_publishable_example",
            )
            save_supabase_config(saved, paths)

            self.assertEqual(load_supabase_config(paths), saved)

            overridden = load_supabase_config(
                paths,
                {
                    "CODEX_SYNC_SUPABASE_URL": "https://override.supabase.co",
                    "CODEX_SYNC_SUPABASE_KEY": "sb_publishable_override",
                },
            )
            self.assertEqual(overridden.project_url, "https://override.supabase.co")

    def test_client_rejects_secret_and_service_role_keys(self) -> None:
        encoded = base64.urlsafe_b64encode(
            json.dumps({"role": "service_role"}).encode("utf-8")
        ).decode("ascii").rstrip("=")
        service_jwt = f"header.{encoded}.signature"

        self.assertTrue(is_forbidden_client_key("sb_secret_example"))
        self.assertTrue(is_forbidden_client_key(service_jwt))
        with self.assertRaises(ValueError):
            SupabaseConfig("https://example.supabase.co", service_jwt)


class SyncStateTests(unittest.TestCase):
    def test_auth_session_is_encrypted_and_device_id_is_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = make_paths(Path(temp_dir))
            state = SyncStateStore(paths, XorProtector())
            session = AuthSession.from_payload(session_payload())
            state.save_auth_session(session)

            raw_db = paths.state_db_path.read_bytes()
            self.assertNotIn(session.access_token.encode("utf-8"), raw_db)
            self.assertNotIn(session.refresh_token.encode("utf-8"), raw_db)
            self.assertEqual(state.load_auth_session(), session)

            first = state.get_or_create_device(__version__)
            second = SyncStateStore(paths, XorProtector()).get_or_create_device(__version__)
            self.assertEqual(first.id, second.id)
            self.assertEqual(first.client_version, __version__)

            with closing(sqlite3.connect(paths.state_db_path)) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
    def test_windows_dpapi_round_trip(self) -> None:
        protector = WindowsDpapiProtector()
        plaintext = b"codex-sync-refresh-token"
        protected = protector.protect(plaintext)

        self.assertNotEqual(protected, plaintext)
        self.assertEqual(protector.unprotect(protected), plaintext)


class AuthAndDeviceTests(unittest.TestCase):
    def test_sign_in_restore_and_sign_out_never_expose_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = make_paths(Path(temp_dir))
            state = SyncStateStore(paths, XorProtector())
            transport = FakeTransport(
                [
                    json_response(200, session_payload()),
                    json_response(200, user_payload()),
                    json_response(204, {}),
                ]
            )
            client = SupabaseClient(
                SupabaseConfig("https://example.supabase.co", "sb_publishable_example"),
                transport=transport,
            )
            auth = AuthService(client, state)

            session = auth.sign_in("USER@example.com", "secret-password")
            account = auth.current_account()
            signed_out = auth.sign_out()

            self.assertEqual(account, session.user)
            self.assertTrue(signed_out)
            self.assertIsNone(state.load_auth_session())
            self.assertNotIn("access_token", json.dumps(session.public_dict()))
            self.assertNotIn("refresh_token", json.dumps(session.public_dict()))
            self.assertEqual(
                transport.requests[0]["body"],
                {"email": "user@example.com", "password": "secret-password"},
            )
            self.assertEqual(
                transport.requests[1]["headers"]["Authorization"],
                "Bearer access-token-value",
            )

    def test_expired_session_refreshes_before_get_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = make_paths(Path(temp_dir))
            state = SyncStateStore(paths, XorProtector())
            expired = AuthSession(
                access_token="expired-access",
                refresh_token="old-refresh",
                expires_at=utc_now() - timedelta(seconds=1),
                token_type="bearer",
                user=UserAccount.from_payload(user_payload()),
            )
            state.save_auth_session(expired)
            transport = FakeTransport(
                [
                    json_response(
                        200,
                        session_payload(
                            access_token="new-access",
                            refresh_token="new-refresh",
                        ),
                    ),
                    json_response(200, user_payload()),
                ]
            )
            auth = AuthService(
                SupabaseClient(
                    SupabaseConfig("https://example.supabase.co", "sb_publishable_example"),
                    transport=transport,
                ),
                state,
            )

            restored = auth.restore_session()

            self.assertIsNotNone(restored)
            self.assertEqual(restored.access_token, "new-access")
            self.assertIn("grant_type=refresh_token", transport.requests[0]["url"])
            self.assertEqual(transport.requests[1]["headers"]["Authorization"], "Bearer new-access")

    def test_signup_without_session_requires_email_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = SyncStateStore(make_paths(Path(temp_dir)), XorProtector())
            transport = FakeTransport([json_response(200, {"user": user_payload()})])
            auth = AuthService(
                SupabaseClient(
                    SupabaseConfig("https://example.supabase.co", "sb_publishable_example"),
                    transport=transport,
                ),
                state,
            )

            result = auth.sign_up("user@example.com", "secret-password")

            self.assertTrue(result.email_confirmation_required)
            self.assertIsNone(result.session)
            self.assertIsNone(state.load_auth_session())

    def test_signout_network_failure_still_clears_local_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = SyncStateStore(make_paths(Path(temp_dir)), XorProtector())
            state.save_auth_session(AuthSession.from_payload(session_payload()))
            transport = FakeTransport([json_response(500, {"message": "temporary failure"})])
            auth = AuthService(
                SupabaseClient(
                    SupabaseConfig("https://example.supabase.co", "sb_publishable_example"),
                    transport=transport,
                ),
                state,
            )

            self.assertFalse(auth.sign_out())
            self.assertIsNone(state.load_auth_session())

    def test_error_message_redacts_password_and_token_values(self) -> None:
        password = "password-that-must-not-leak"
        token = "token-that-must-not-leak"
        transport = FakeTransport(
            [
                json_response(
                    400,
                    {"message": f"bad password {password} and token {token}"},
                )
            ]
        )
        client = SupabaseClient(
            SupabaseConfig("https://example.supabase.co", "sb_publishable_example"),
            transport=transport,
        )

        with self.assertRaises(SupabaseError) as caught:
            client.request_json(
                "POST",
                "/auth/v1/token",
                payload={"password": password, "refresh_token": token},
            )

        message = str(caught.exception)
        self.assertNotIn(password, message)
        self.assertNotIn(token, message)
        self.assertIn("[REDACTED]", message)

    def test_device_service_persists_and_upserts_v2_device(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = SyncStateStore(make_paths(Path(temp_dir)), XorProtector())
            state.save_auth_session(AuthSession.from_payload(session_payload()))
            transport = FakeTransport(
                [
                    json_response(200, user_payload()),
                    json_response(
                        201,
                        [
                            {
                                "id": "device-cloud-id",
                                "device_name": "Test",
                            }
                        ],
                    ),
                    json_response(200, user_payload()),
                    json_response(200, [{"id": "device-cloud-id"}]),
                    json_response(200, user_payload()),
                    json_response(
                        201,
                        [
                            {
                                "id": "device-cloud-id",
                                "last_backup_at": "2026-08-21T00:00:00+00:00",
                            }
                        ],
                    ),
                ]
            )
            client = SupabaseClient(
                SupabaseConfig("https://example.supabase.co", "sb_publishable_example"),
                transport=transport,
            )
            auth = AuthService(client, state)
            devices = DeviceService(auth, state, SupabaseDeviceRepository(client))

            registered = devices.register_current_device()
            listed = devices.list_devices()
            backed_up = devices.record_successful_backup()

            self.assertEqual(registered["id"], "device-cloud-id")
            self.assertEqual(listed, [{"id": "device-cloud-id"}])
            self.assertEqual(backed_up["id"], "device-cloud-id")
            upsert_request = transport.requests[1]
            self.assertIn("on_conflict=user_id%2Cid", upsert_request["url"])
            self.assertEqual(upsert_request["body"]["user_id"], user_payload()["id"])
            self.assertEqual(upsert_request["body"]["client_version"], __version__)
            self.assertIsNotNone(state.get_device().last_backup_at)
            self.assertIsNotNone(transport.requests[5]["body"]["last_backup_at"])


if __name__ == "__main__":
    unittest.main()
