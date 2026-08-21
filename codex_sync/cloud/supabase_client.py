from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from codex_sync.config import SupabaseConfig


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        request = Request(url=url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except HTTPError as exc:
            return HttpResponse(
                status=int(exc.code),
                body=exc.read(),
                headers=dict(exc.headers.items()) if exc.headers else {},
            )
        except URLError as exc:
            raise SupabaseError("Supabase network request failed", cause=exc) from exc


class SupabaseError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.__cause__ = cause


class SupabaseClient:
    def __init__(
        self,
        config: SupabaseConfig,
        transport: HttpTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibTransport()
        self.timeout_seconds = timeout_seconds

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        access_token: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        expected_statuses: tuple[int, ...] = (200,),
    ) -> Any:
        body = None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "apikey": self.config.public_key,
            "Authorization": f"Bearer {access_token or self.config.public_key}",
        }
        if extra_headers:
            headers.update(extra_headers)
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        response = self.transport.request(
            method,
            f"{self.config.project_url}{path}",
            headers,
            body,
            self.timeout_seconds,
        )
        decoded = decode_json(response.body)
        if response.status not in expected_statuses:
            sensitive_values = [
                self.config.public_key,
                access_token or "",
                *collect_sensitive_values(payload),
            ]
            raise error_from_response(response.status, decoded, sensitive_values)
        return decoded


def decode_json(body: bytes) -> Any:
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupabaseError("Supabase returned an invalid JSON response", cause=exc) from exc


def collect_sensitive_values(payload: object | None) -> list[str]:
    values: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key).lower()
            if isinstance(value, str) and any(
                marker in normalized_key
                for marker in ("password", "token", "secret", "key", "authorization")
            ):
                values.append(value)
            else:
                values.extend(collect_sensitive_values(value))
    elif isinstance(payload, list):
        for value in payload:
            values.extend(collect_sensitive_values(value))
    return values


def redact_values(text: str, values: list[str]) -> str:
    redacted = text
    for value in sorted({value for value in values if value}, key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def error_from_response(
    status_code: int,
    payload: Any,
    sensitive_values: list[str] | None = None,
) -> SupabaseError:
    if isinstance(payload, dict):
        message = (
            payload.get("msg")
            or payload.get("message")
            or payload.get("error_description")
            or payload.get("error")
        )
        code = payload.get("code") or payload.get("error_code")
    else:
        message = None
        code = None
    safe_message = str(message) if message else f"Supabase request failed with HTTP {status_code}"
    safe_message = redact_values(safe_message, sensitive_values or [])
    return SupabaseError(
        safe_message,
        status_code=status_code,
        code=str(code) if code else None,
    )
