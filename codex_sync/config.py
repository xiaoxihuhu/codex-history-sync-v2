from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from codex_sync.atomic_io import atomic_write_json

SUPABASE_URL_ENV = "CODEX_SYNC_SUPABASE_URL"
SUPABASE_KEY_ENV = "CODEX_SYNC_SUPABASE_KEY"


@dataclass(frozen=True)
class AppPaths:
    root: Path
    config_path: Path
    state_db_path: Path
    logs_dir: Path

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class SupabaseConfig:
    project_url: str
    public_key: str

    def __post_init__(self) -> None:
        normalized_url = self.project_url.rstrip("/")
        parsed = urlparse(normalized_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Supabase project URL must be an absolute HTTPS URL")
        if parsed.path not in ("", "/"):
            raise ValueError("Supabase project URL must not include an API path")
        if not self.public_key.strip():
            raise ValueError("Supabase publishable/anon key is required")
        if is_forbidden_client_key(self.public_key):
            raise ValueError("A Supabase secret/service-role key cannot be used by the desktop client")
        object.__setattr__(self, "project_url", normalized_url)
        object.__setattr__(self, "public_key", self.public_key.strip())

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def default_app_paths(environment: Mapping[str, str] | None = None) -> AppPaths:
    values = environment or os.environ
    local_app_data = values.get("LOCALAPPDATA")
    root = Path(local_app_data) / "CodexHistorySync" if local_app_data else Path.home() / ".codex-history-sync"
    return AppPaths(
        root=root,
        config_path=root / "config.json",
        state_db_path=root / "sync_state.sqlite",
        logs_dir=root / "logs",
    )


def jwt_role(value: str) -> str | None:
    parts = value.split(".")
    if len(parts) != 3:
        return None
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    role = payload.get("role") if isinstance(payload, dict) else None
    return str(role) if role else None


def is_forbidden_client_key(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized.startswith(("sb_secret_", "service_role")):
        return True
    role = jwt_role(value)
    return role in {"service_role", "supabase_admin"}


def save_supabase_config(config: SupabaseConfig, paths: AppPaths | None = None) -> Path:
    app_paths = paths or default_app_paths()
    app_paths.ensure()
    atomic_write_json(
        app_paths.config_path,
        {
            "version": 1,
            "supabase": config.to_dict(),
        },
    )
    return app_paths.config_path


def load_supabase_config(
    paths: AppPaths | None = None,
    environment: Mapping[str, str] | None = None,
) -> SupabaseConfig:
    app_paths = paths or default_app_paths(environment)
    values = environment or os.environ
    env_url = values.get(SUPABASE_URL_ENV)
    env_key = values.get(SUPABASE_KEY_ENV)
    if env_url or env_key:
        if not env_url or not env_key:
            raise RuntimeError(f"{SUPABASE_URL_ENV} and {SUPABASE_KEY_ENV} must be set together")
        return SupabaseConfig(env_url, env_key)

    if not app_paths.config_path.exists():
        raise RuntimeError(
            "Supabase is not configured. Set the Codex Sync project URL and publishable/anon key first."
        )
    payload = json.loads(app_paths.config_path.read_text(encoding="utf-8"))
    supabase = payload.get("supabase") if isinstance(payload, dict) else None
    if not isinstance(supabase, dict):
        raise RuntimeError("Supabase configuration is missing or invalid")
    return SupabaseConfig(
        project_url=str(supabase.get("project_url") or ""),
        public_key=str(supabase.get("public_key") or ""),
    )
