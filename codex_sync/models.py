from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class UserAccount:
    id: str
    email: str | None
    created_at: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "UserAccount":
        user_id = str(payload.get("id") or "").strip()
        if not user_id:
            raise ValueError("Supabase user payload is missing id")
        email = payload.get("email")
        return cls(
            id=user_id,
            email=str(email) if email else None,
            created_at=str(payload.get("created_at")) if payload.get("created_at") else None,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AuthSession:
    access_token: str
    refresh_token: str
    expires_at: datetime
    token_type: str
    user: UserAccount

    @classmethod
    def from_payload(cls, payload: dict[str, Any], now: datetime | None = None) -> "AuthSession":
        access_token = str(payload.get("access_token") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        if not access_token or not refresh_token:
            raise ValueError("Supabase Auth response is missing session tokens")

        current_time = now or utc_now()
        raw_expires_at = payload.get("expires_at")
        if raw_expires_at is not None:
            expires_at = datetime.fromtimestamp(float(raw_expires_at), tz=UTC)
        else:
            expires_in = int(payload.get("expires_in") or 0)
            if expires_in <= 0:
                raise ValueError("Supabase Auth response is missing token expiry")
            expires_at = datetime.fromtimestamp(current_time.timestamp() + expires_in, tz=UTC)

        user_payload = payload.get("user")
        if not isinstance(user_payload, dict):
            raise ValueError("Supabase Auth response is missing user data")
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            token_type=str(payload.get("token_type") or "bearer"),
            user=UserAccount.from_payload(user_payload),
        )

    @classmethod
    def from_storage_dict(cls, payload: dict[str, Any]) -> "AuthSession":
        user = payload.get("user")
        if not isinstance(user, dict):
            raise ValueError("Stored Auth session is missing user data")
        return cls(
            access_token=str(payload["access_token"]),
            refresh_token=str(payload["refresh_token"]),
            expires_at=parse_datetime(str(payload["expires_at"])),
            token_type=str(payload.get("token_type") or "bearer"),
            user=UserAccount.from_payload(user),
        )

    def to_storage_dict(self) -> dict[str, object]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
            "token_type": self.token_type,
            "user": self.user.to_dict(),
        }

    def public_dict(self) -> dict[str, object]:
        return {
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
            "token_type": self.token_type,
            "user": self.user.to_dict(),
        }


@dataclass(frozen=True)
class DeviceIdentity:
    id: str
    device_name: str
    os_name: str
    os_version: str
    client_version: str
    first_registered_at: str
    last_seen_at: str | None = None
    last_backup_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
