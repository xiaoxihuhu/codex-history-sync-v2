"""Supabase Auth, database, and Storage clients."""

from .auth import AuthService
from .attachments import SupabaseAttachmentRepository
from .backup import SupabaseManualUploadRepository
from .devices import DeviceService, SupabaseDeviceRepository
from .restore import SupabaseRestoreRepository
from .supabase_client import SupabaseClient, SupabaseError
from .snapshots import SupabaseSnapshotRepository
from .native_state import (
    EncryptedNativeSnapshotRepository,
    FakeSupabaseNativeStateRepository,
    InMemoryNativeStateRepository,
    NativeStateRepository,
)
from .workspaces import SupabaseWorkspaceRepository

__all__ = [
    "AuthService",
    "DeviceService",
    "SupabaseAttachmentRepository",
    "SupabaseManualUploadRepository",
    "SupabaseRestoreRepository",
    "SupabaseWorkspaceRepository",
    "SupabaseClient",
    "SupabaseDeviceRepository",
    "SupabaseError",
    "SupabaseSnapshotRepository",
    "EncryptedNativeSnapshotRepository",
    "FakeSupabaseNativeStateRepository",
    "InMemoryNativeStateRepository",
    "NativeStateRepository",
]
