from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from codex_sync.cloud.supabase_client import SupabaseClient, SupabaseError
from codex_sync.models import AuthSession, UserAccount, utc_now
from codex_sync.sync.state import SyncStateStore


@dataclass(frozen=True)
class SignUpResult:
    user: UserAccount
    session: AuthSession | None
    email_confirmation_required: bool

    def public_dict(self) -> dict[str, object]:
        return {
            "user": self.user.to_dict(),
            "session": self.session.public_dict() if self.session else None,
            "email_confirmation_required": self.email_confirmation_required,
        }


class AuthService:
    def __init__(self, client: SupabaseClient, state: SyncStateStore) -> None:
        self.client = client
        self.state = state

    def sign_up(self, email: str, password: str) -> SignUpResult:
        payload = self.client.request_json(
            "POST",
            "/auth/v1/signup",
            payload={"email": normalized_email(email), "password": require_password(password)},
            expected_statuses=(200, 201),
        )
        if not isinstance(payload, dict):
            raise SupabaseError("Supabase signup returned an invalid response")
        user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else payload
        if not isinstance(user_payload, dict):
            raise SupabaseError("Supabase signup response is missing user data")
        user = UserAccount.from_payload(user_payload)
        session = session_from_optional_payload(payload)
        if session:
            self.state.save_auth_session(session)
        return SignUpResult(
            user=user,
            session=session,
            email_confirmation_required=session is None,
        )

    def sign_in(self, email: str, password: str) -> AuthSession:
        payload = self.client.request_json(
            "POST",
            "/auth/v1/token?grant_type=password",
            payload={"email": normalized_email(email), "password": require_password(password)},
            expected_statuses=(200,),
        )
        session = AuthSession.from_payload(require_mapping(payload, "signin"))
        self.state.save_auth_session(session)
        return session

    def refresh_session(self, session: AuthSession | None = None) -> AuthSession:
        current = session or self.state.load_auth_session()
        if current is None:
            raise RuntimeError("No saved Codex Sync login session")
        payload = self.client.request_json(
            "POST",
            "/auth/v1/token?grant_type=refresh_token",
            payload={"refresh_token": current.refresh_token},
            expected_statuses=(200,),
        )
        refreshed = AuthSession.from_payload(require_mapping(payload, "refresh"))
        self.state.save_auth_session(refreshed)
        return refreshed

    def restore_session(self, refresh_skew_seconds: int = 60) -> AuthSession | None:
        session = self.state.load_auth_session()
        if session is None:
            return None
        if session.expires_at <= utc_now() + timedelta(seconds=refresh_skew_seconds):
            session = self.refresh_session(session)
        try:
            user = self.get_user(session)
        except SupabaseError as exc:
            if exc.status_code not in (401, 403):
                raise
            session = self.refresh_session(session)
            user = self.get_user(session)
        if user != session.user:
            session = AuthSession(
                access_token=session.access_token,
                refresh_token=session.refresh_token,
                expires_at=session.expires_at,
                token_type=session.token_type,
                user=user,
            )
            self.state.save_auth_session(session)
        return session

    def get_user(self, session: AuthSession) -> UserAccount:
        payload = self.client.request_json(
            "GET",
            "/auth/v1/user",
            access_token=session.access_token,
            expected_statuses=(200,),
        )
        return UserAccount.from_payload(require_mapping(payload, "get user"))

    def current_account(self) -> UserAccount | None:
        session = self.restore_session()
        return session.user if session else None

    def sign_out(self) -> bool:
        session = self.state.load_auth_session()
        remote_signed_out = False
        try:
            if session is not None:
                try:
                    self.client.request_json(
                        "POST",
                        "/auth/v1/logout",
                        access_token=session.access_token,
                        expected_statuses=(200, 204),
                    )
                    remote_signed_out = True
                except SupabaseError:
                    remote_signed_out = False
        finally:
            self.state.clear_auth_session()
        return remote_signed_out


def normalized_email(email: str) -> str:
    value = email.strip().lower()
    if "@" not in value or value.startswith("@") or value.endswith("@"):
        raise ValueError("A valid email address is required")
    return value


def require_password(password: str) -> str:
    if len(password) < 6:
        raise ValueError("Password must contain at least 6 characters")
    return password


def require_mapping(payload: Any, operation: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SupabaseError(f"Supabase {operation} returned an invalid response")
    return payload


def session_from_optional_payload(payload: dict[str, Any]) -> AuthSession | None:
    if payload.get("access_token") and payload.get("refresh_token"):
        return AuthSession.from_payload(payload)
    session = payload.get("session")
    if isinstance(session, dict):
        return AuthSession.from_payload(session)
    return None
