from __future__ import annotations

from urllib.parse import quote

from codex_sync.cloud.supabase_client import SupabaseClient, SupabaseError

DEFAULT_BUCKET = "codex-history-sync"


class SupabaseStorage:
    def __init__(self, client: SupabaseClient, bucket: str = DEFAULT_BUCKET) -> None:
        self.client = client
        self.bucket = bucket

    def upload(
        self,
        object_path: str,
        content: bytes,
        *,
        content_type: str,
        access_token: str,
    ) -> None:
        encoded_bucket = quote(self.bucket, safe="")
        encoded_path = quote(object_path, safe="/")
        response = self.client.request_bytes(
            "POST",
            f"/storage/v1/object/{encoded_bucket}/{encoded_path}",
            content=content,
            content_type=content_type,
            access_token=access_token,
            extra_headers={"x-upsert": "true"},
            expected_statuses=(200, 201),
        )
        if response not in ({}, None) and not isinstance(response, dict):
            raise SupabaseError("Supabase Storage upload returned an invalid response")

    def download(self, object_path: str, *, access_token: str) -> bytes:
        encoded_bucket = quote(self.bucket, safe="")
        encoded_path = quote(object_path, safe="/")
        return self.client.download_bytes(
            f"/storage/v1/object/{encoded_bucket}/{encoded_path}",
            access_token=access_token,
        )
