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
    NativeAuthMismatchError,
    NativeCloudExportIncomplete,
    NativeCloudExportNotFound,
    NativeCloudIntegrityError,
    NativeCloudRetryPolicy,
    NativeCloudSchemaNotInstalled,
    NativeCloudSchemaVerifier,
    NativeStateRepository,
    SupabaseNativeStateRepository,
)
from .native_backup import NativeCloudBackupResult, NativeCloudBackupService, link_snapshot_native_export
from .native_manifest import NativeExportManifest
from .native_restore import NativeCloudRestoreResult, NativeCloudRestoreService
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
    "SupabaseNativeStateRepository",
    "NativeAuthMismatchError",
    "NativeCloudExportIncomplete",
    "NativeCloudExportNotFound",
    "NativeCloudIntegrityError",
    "NativeCloudRetryPolicy",
    "NativeCloudSchemaNotInstalled",
    "NativeCloudSchemaVerifier",
    "NativeCloudBackupResult",
    "NativeCloudBackupService",
    "NativeExportManifest",
    "NativeCloudRestoreResult",
    "NativeCloudRestoreService",
    "link_snapshot_native_export",
]
